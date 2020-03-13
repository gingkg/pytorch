import tempfile
import shutil
from string import Template
import unittest
from torch.testing._internal.common_cuda import TEST_CUDA

import torch
import torch.testing._internal.common_nn as common_nn
from cpp_api_parity.utils import TorchNNTestParams

# DO NOW:
# yf225 TODO: can we make things significantly simpler to understand, even if that means doing some manual work to every test dict?
# TODO: we should write the CPP ctor args and input args DIRECTLY in the test dict in common_nn, in order to significant simplify logic in this file

# Check 1: Module implementation correctness check:

# Step 1: Translate ctor args from Python layer to C++ layer
# Step 2: Construct a C++ layer, run forward and backward on it, save all its params/buffers/gradients into a ScriptModule
# Step 3: Load that ScriptModule into Python, and compare output/params/buffers/gradients with Python layer (forward and backward)

# yf225 TODO: move to common utils?
devices = ['cpu', 'cuda']

# yf225 TODO: move to common utils?
TORCH_NN_MODULE_COMMON_TEST_HARNESS = """\n
#include <torch/script.h>
"""

TORCH_NN_MODULE_TEST_FORWARD_BACKWARD = Template("""\n
void ${module_variant_name}_test_forward_backward(const std::string& device) {
  torch::manual_seed(0);

  ${module_qualified_name} module${cpp_constructor_args};
  module->to(device);

  // Forward pass
  auto cpp_output = module(${cpp_input_args});

  // Save the output into an IValue to be compared in Python later
  torch::jit::save(
    "${cpp_output_tmp_folder}_${module_variant_name}_forward_output.pt",
    torch::IValue(cpp_output));

  // Backward pass
  cpp_output.sum().backward();

  // Put all gradients into a c10::Dict, save it into an IValue to be compared in Python later
  // yf225 TODO: how are nested parameters named? Do they have proper prefix to avoid collision?
  c10::Dict<torch::IValue, torch::IValue> grad_dict;
  for (const auto& param : module->named_parameters()) {
    grad_dict.insert(torch::IValue(param.key()), torch::IValue(param.value()->grad()));
  }
  torch::jit::save(
    "${cpp_output_tmp_folder}_${module_variant_name}_backward_grad_dict.pt",
    grad_dict);
}
""")

def _compile_cpp_code_inline(self, name, cpp_sources, functions):
    # Just-in-time compile the C++ test code
    cpp_module = torch.utils.cpp_extension.load_inline(
        name=name,
        cpp_sources=cpp_sources,
        functions=functions,
        verbose=False,
    )
    return cpp_module

def _test_torch_nn_module_variant(test_params):
    # yf225 TODO: move to common utils?
    # yf225 TODO: we should check in a copy of the generated source code, and then run consistency test (compare old vs. newly generated)
    def generate_test_cpp_sources(test_params, template):
      test_cpp_sources = template.substitute(
        module_variant_name=test_params.module_variant_name,
        module_qualified_name='torch::nn::{}'.format(test_params.module_name),
        cpp_constructor_args=test_params.cpp_constructor_args,
        input_args=',\n'.join(test_params.input_args),
        cpp_output_tmp_folder=test_params.cpp_output_tmp_folder,
      )
      print(test_cpp_sources)
      return test_cpp_sources

    def test_forward_backward(test_params):
      # yf225 TODO: this function should handle all of forward-backward testing logic and do the result check
      torch.manual_seed(0)

      device = test_params.device
      python_constructor = test_params.test_instance.constructor
      python_constructor_args = test_params.test_instance.constructor_args
      python_input_args = test_params.test_instance._get_input()

      module = python_constructor(*python_constructor_args).to(device)
      python_output = module(*input_args)
      python_output.sum().backward()

      cpp_sources = TORCH_NN_MODULE_COMMON_TEST_HARNESS + \
        module_metadata.cpp_sources + \
        generate_test_cpp_sources(
          test_params=test_params, template=TORCH_NN_MODULE_TEST_FORWARD_BACKWARD)

      cpp_module = _compile_cpp_code_inline(
        name=test_params.module_variant_name,
        cpp_sources=cpp_sources,
        functions=['{}_test_{}'.format(test_params.module_variant_name, 'test_forward_backward')])

      cpp_test_name = '{}_test_{}'.format(test_params.module_variant_name, 'test_forward_backward')
      cpp_test_fn = getattr(cpp_module, cpp_test_name)
      if not test_params.has_parity:
        with self.assertRaisesRegex(RuntimeError, "Parity test failed"):
          cpp_test_fn("cpu")
      else:
        cpp_test_fn("cpu")
        ivalue = torch.jit.load("{}_{}_forward_output.pt", test_params.cpp_output_tmp_folder, test_params.module_variant_name)
        print(ivalue)

      # Remove temporary folder that stores C++ outputs
      shutil.rmtree(test_params.cpp_output_tmp_folder)

    test_basic_methods(test_params)

# yf225 TODO: move to common utils?
def _compute_module_name(test_params_dict):
    fullname = test_params_dict.get('fullname', None)
    if fullname:
        # NOTE: This doesn't work for some of the `wrap_functional` module tests such as "interpolate_nearest_1d",
        # because in that case the module `interpolate` is not in `torch.nn` but rather in `torch.nn.functional`.
        # We will fix this when we have parity tests for `torch.nn.functional` modules.
        module_name = fullname.split('_')[0]
    else:
        module_name = test_params_dict.get('module_name')
    return module_name

# yf225 TODO: move to common utils?
def _process_test_params_for_module(test_params_dict, module_metadata, device, is_criterion):
    module_name = _compute_module_name(test_params_dict)
    test_params_dict['constructor'] = test_params_dict.get('constructor', getattr(torch.nn, module_name))
    if is_criterion:
        test = common_nn.CriterionTest(**test_params_dict)
    else:
        test = common_nn.ModuleTest(**test_params_dict)
    # yf225 TODO: can we remove the magic number `5` here?
    module_variant_name = test.get_name()[5:] + (('_' + device) if device != 'cpu' else '')    

    return TorchNNTestParams(
        module_name=module_name,
        module_variant_name=module_variant_name,
        test_instance=test,
        cpp_constructor_args=test_params_dict.get('cpp_constructor_args'),
        cpp_input_args=test_params_dict.get('cpp_input_args'),
        has_parity=test_params_dict.get('has_parity', True),
        device=device,
        cpp_output_tmp_folder=tempfile.mkdtemp(),
    )

# yf225 TODO: move to common utils?
def has_test(unit_test_class, test_name):
    return hasattr(unit_test_class, test_name)

# yf225 TODO: move to common utils?
def add_test(unit_test_class, test_name, test_fn):
    if has_test(unit_test_class, test_name):
        raise RuntimeError("Found two tests with the same name: " + test_name)
    setattr(unit_test_class, test_name, test_fn)

def add_torch_nn_module_impl_parity_tests(parity_table, unit_test_class, torch_nn_modules, module_tests, is_criterion):
  torch_nn_test_params_map = {}
  for test_params_dict in module_tests:
    # Skip all `torch.nn.functional` tests, since they are handled by another test suite.
    if 'FunctionalModule' in str(test_params_dict.get('constructor', '')):
      continue

    module_name = _compute_module_name(test_params_dict)

    assert hasattr(torch.nn, module_name), \
      "`torch.nn` doesn't have module `{}`. ".format(module_name) + \
      "If you are adding a new test, please set `fullname` using format `ModuleName_desc`, " + \
      "or set `module_name` using format `ModuleName`."

    module_full_name = 'torch::nn::' + module_name
    # If equivalent module in C++ frontend doesn't exist, we don't do the parity test.
    if module_full_name not in parity_table['torch::nn']:
      continue

    has_impl_parity, _ = parity_table['torch::nn'][module_full_name]

    def add_variant_test_for_module(module_name, test_params_dict, has_impl_parity, torch_nn_modules):
      module_metadata = torch_nn_modules.module_metadata_map[module_name]
      for device in devices:
        test_params = _process_test_params_for_module(
          test_params_dict=test_params_dict,
          module_metadata=module_metadata,
          device=device,
          is_criterion=is_criterion)
        test_name = 'test_torch_nn_{}'.format(test_params.module_variant_name)
        torch_nn_test_params_map[test_name] = test_params

        def test_fn():
          _test_torch_nn_module_variant(test_params=torch_nn_test_params_map[self._testMethodName])

        if device == 'cuda':
          test_fn = unittest.skipIf(not TEST_CUDA, "CUDA unavailable")(test_fn)

        # If `Implementation Parity` entry in parity table for this module is `No`,
        # we mark the test as expected failure.
        if not has_impl_parity:
          test_fn = unittest.expectedFailure(test_fn)

        add_test(unit_test_class, test_name, test_fn)

    add_variant_test_for_module(
      module_name=module_name,
      test_params_dict=test_params_dict,
      has_impl_parity=has_impl_parity,
      torch_nn_modules=torch_nn_modules)

def add_tests(unit_test_class, module_tests, criterion_tests, torch_nn_modules, parity_table):
  add_torch_nn_module_impl_parity_tests(
    parity_table=parity_table,
    unit_test_class=unit_test_class,
    torch_nn_modules=torch_nn_modules,
    module_tests=module_tests,
    is_criterion=False)

  add_torch_nn_module_impl_parity_tests(
    parity_table=parity_table,
    unit_test_class=unit_test_class,
    torch_nn_modules=torch_nn_modules,
    module_tests=criterion_tests,
    is_criterion=True)
