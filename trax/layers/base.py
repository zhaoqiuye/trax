# coding=utf-8
# Copyright 2020 The Trax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Base layer class."""

import copy
import inspect
import pickle
import random
import traceback

import jax
import numpy as np
import tensorflow as tf

from trax import math
from trax.math import nested_map
from trax.math import numpy as jnp
from trax.shapes import ShapeDtype
from trax.shapes import signature


# TODO(lukaszkaiser): distinguish the 2 kinds below and add more tests.
EMPTY_WEIGHTS = ()    # Used for layers that have no trainable weights.
EMPTY_STATE = ()      # Used for layers that have no non-trainable state.
GET_WEIGHTS_FROM_CACHE = ()  # Used to mark that the weights are shared.
GET_STATE_FROM_CACHE = ()    # Used to mark that the state is shared.


class Layer:
  """Base class for composable layers in a deep learning network.

  Layers are the basic building blocks for deep learning models. A Trax layer
  computes a function from zero or more inputs to zero or more outputs,
  optionally using trainable weights (common) and non-parameter state (not
  common). Authors of new layer subclasses typically override at most two
  methods of the base `Layer` class:

    `forward(inputs, weights)`:
      Computes this layer's output as part of a forward pass through the model.

    `new_weights(self, input_signature)`:
      Returns new weights suitable for inputs with the given signature.

  A small subset of layer types are combinators -- they organize the computation
  of their sublayers, e.g., applying their sublayers in series or in parallel.

  All layers have the following properties, with default values implemented
  in the base `Layer` class:

    - `n_in`: int (default 1)
    - `n_out`: int (default 1)
    - `weights`: tuple (default empty -- the layer has no weights)
    - `state`: tuple (default empty -- the layer has no non-parameter state)
    - `sublayers`: tuple (default empty -- the layer has no sublayers)

  The inputs to a layer are tensors, packaged according to how many there are:

    - `n_in = 0`: an empty tuple
    - `n_in = 1`: one tensor (NOT wrapped in a tuple)
    - `n_in > 1`: a tuple of tensors

  (The special treatment of the single-input case is meant to simplify the
  work of layer writers; this design choice may be revisited in the future.)

  The outputs from a layer are also tensors, packaged the same as layer inputs:

    - `n_out = 0`: an empty tuple
    - `n_out = 1`: the tensor (NOT wrapped in a tuple)
    - `n_out > 1`: a tuple of tensors

  The Trax runtime maintains a data stack with which layer calls are composed.
  For more complex data network architectures, possibly involving multiple data
  flows, one can view each layer as a function from stack state to stack state,
  where the function's inputs are a slice from the stack, and the function's
  outputs are spliced back into the stack.
  """

  def __init__(self, n_in=1, n_out=1, name=None):
    """Creates a partially initialized, unconnected layer instance.

    Args:
      n_in: Number of inputs expected by this layer.
      n_out: Number of outputs promised by this layer.
      name: Class-like name for this layer; for use in debugging.
    """
    self._n_in = n_in
    self._n_out = n_out
    self._name = name or self.__class__.__name__
    self._sublayers = ()  # Default is no sublayers.
    # This may run before some backends (e.g. JAX) are initialized, so we use
    # Python `int` here instead of `math.random.get_prng` (also note that
    # different backends' `get_prng` may return different shapes so they can't
    # be used interchangeably).
    self._rng = random.randint(0, 2**31 - 1)
    self._weights = EMPTY_WEIGHTS  # cached weights
    self._state = EMPTY_STATE
    # record root call site for custom error messages:
    frame = _find_frame(inspect.currentframe())
    # Turns out that frame can mutate in time, so we just copy what we need.
    self._caller = {'filename': copy.copy(frame.f_code.co_filename),
                    'lineno': int(frame.f_lineno)}
    del frame  # Just in case.
    self._init_cached = False
    self._jit_cache = {}

  def __repr__(self):
    def indent_string(x):
      return '  ' + x.replace('\n', '\n  ')
    name_str = self._name
    n_in, n_out = self.n_in, self.n_out
    if n_in != 1: name_str += f'_in{n_in}'
    if n_out != 1: name_str += f'_out{n_out}'
    objs = self.sublayers
    if objs:
      objs_str = '\n'.join(indent_string(str(x)) for x in objs)
      return f'{name_str}[\n{objs_str}\n]'
    else:
      return name_str

  def __call__(self, x, weights=None, state=None, rng=None, n_accelerators=0):
    """Makes layers callable; for use in tests or interactive settings.

    This convenience method helps library users play with, test, or otherwise
    probe the behavior of layers outside of a full training environment. It
    presents the layer as callable function from inputs to outputs, with the
    option of manually specifying weights and non-parameter state per individual
    call. For convenience, weights and non-parameter state are cached per layer
    instance, starting from default values of `EMPTY_WEIGHTS` and `EMPTY_STATE`,
    and acquiring non-empty values either by initialization or from values
    explicitly provided via the weights and state keyword arguments.

    Args:
      x: Zero or more input tensors, packaged as described in the `Layer` class
          docstring.
      weights: Weights or `None`; if `None`, use self's cached weights value.
      state: State or `None`; if `None`, use self's cached state value.
      rng: Single-use random number generator (JAX PRNG key), or `None`;
          if `None`, use a default computed from an integer 0 seed.
      n_accelerators: Number of accelerators to target.

    Returns:
      Zero or more output tensors, packaged as described in the `Layer` class
      docstring.
    """
    weights = self.weights if weights is None else weights
    if state is not None:
      self.state = state  # Needed if the model wasn't fully initialized.
    state = self.state
    rng = self.rng if rng is None else rng
    rng = math.random.get_prng(0) if rng is None else rng

    forward_w_s_r = self.pure_fn
    # TODO(lukaszkaiser): n_accelerators is experimental, to decide on API
    if n_accelerators:
      if n_accelerators not in self._jit_cache:
        self._jit_cache[n_accelerators] = (
            jit_forward(forward_w_s_r, n_accelerators))
      forward_w_s_r = self._jit_cache[n_accelerators]
    outputs, new_state = forward_w_s_r(x, weights, state, rng)
    self.state = new_state
    self.weights = weights
    return outputs

  def forward(self, inputs, weights):
    """Computes this layer's output as part of a forward pass through the model.

    Authors of new layer subclasses should override this method to define the
    forward computation that their layer performs. If you need to use
    local non-trainable state or randomness, use `self.rng` for the random seed
    (no need to set it) and use `self.state` for non-trainable state (and set it
    to the new value).

    Args:
      inputs: Zero or more input tensors, packaged as described in the `Layer`
          class docstring.
      weights: A tuple or list of trainable weights, with one element for this
          layer if this layer has no sublayers, or one for each sublayer if
          this layer has sublayers. If a layer (or sublayer) has no trainable
          weights, the corresponding weights element is an empty tuple.

    Returns:
      Zero or more output tensors, packaged as described in the `Layer` class
      docstring.
    """
    raise NotImplementedError

  def new_weights(self, input_signature):
    """Returns new weights suitable for inputs with the given signature.

    Authors of new layer subclasses should override this method if their layer
    uses trainable weights or non-trainable state. To initialize non-trainable
    state, set `self.state` to the intended value.

    Args:
      input_signature: A `ShapeDtype` instance (if this layer takes one input)
          or a list/tuple of `ShapeDtype` instances; signatures of inputs.
    """
    del input_signature
    return EMPTY_WEIGHTS

  @property
  def has_backward(self):
    """Returns `True` if this layer provides its own custom backward pass code.

    A layer subclass that provides custom backward pass code (for custom
    gradients) must override this method to return `True`.
    """
    return False

  def backward(self, inputs, output, grad, weights, state, new_state, rng):
    """Custom backward pass to propagate gradients in a custom way.

    Args:
      inputs: Input tensors; can be a (possibly nested) tuple.
      output: The result of running this layer on inputs.
      grad: Gradient signal computed based on subsequent layers; its structure
          and shape must match output.
      weights: This layer's weights.
      state: This layer's state prior to the current forward pass.
      new_state: This layer's state after the current forward pass.
      rng: Single-use random number generator (JAX PRNG key).

    Returns:
      The custom gradient signal for the input. Note that we need to return
      a gradient for each argument of forward, so it will usually be a tuple
      of signals: the gradient for inputs and weights.
    """
    raise NotImplementedError

  # End of public subclassing interface.
  # Begin public callable interface.

  def init(self, input_signature, rng=None, use_cache=False):
    """Initializes this layer and its sublayers recursively.

    This method is designed to initialize each layer instance once, even if the
    same layer instance occurs in multiple places in the network. This enables
    weight sharing to be implemented as layer sharing.

    Args:
      input_signature: `ShapeDtype` instance (if this layer takes one input)
          or list/tuple of `ShapeDtype` instances.
      rng: Single-use random number generator (JAX PRNG key), or `None`;
          if `None`, use a default computed from an integer 0 seed.
      use_cache: If `True`, and if this layer instance has already been
          initialized elsewhere in the network, then return special marker
          values -- tuple `(GET_WEIGHTS_FROM_CACHE, GET_STATE_FROM_CACHE)`.
          Else return this layer's newly initialized weights and state.

    Returns:
      A `(weights, state)` tuple.
    """
    try:
      if self._init_cached and use_cache:
        return (GET_WEIGHTS_FROM_CACHE, GET_STATE_FROM_CACHE)

      if rng is not None:
        self.rng = rng
      weights = self.new_weights(input_signature)
      self._weights = weights

      if use_cache:
        self._init_cached = True
      else:
        self._clear_init_cache()

      return (weights, self.state)

    except Exception as e:
      name, trace = self._name, _short_traceback(skip=3)
      raise LayerError(name, 'init', self._caller,
                       input_signature, trace) from e

  def init_from_file(self, file_name, weights_only=False):
    """Initializes this layer and its sublayers from a pickled checkpoint.

    In the common case (`weights_only=False`), the file must be a pickled
    dictionary containing items with keys `'weights' and `'state'`, whose
    values have the correct structure for this layer's weights and state.
    If `weights_only` is `True`, the dictionary only needs to have a
    `'weights'` item.

    Args:
      file_name: Name/path of the pickeled weights/state file.
      weights_only: If `True`, initialize only the layer's weights. Else
          initialize both weights and state.
    """
    with tf.io.gfile.GFile(file_name, 'rb') as f:
      dictionary = pickle.load(f)
    self.weights = dictionary['weights']
    if not weights_only:
      self.state = dictionary['state']

  # End of public callable methods.
  # Methods and properties below are reserved for internal use.

  @property
  def n_in(self):
    """Returns how many tensors this layer expects as input."""
    return self._n_in

  @property
  def n_out(self):
    """Returns how many tensors this layer promises as output."""
    return self._n_out

  @property
  def sublayers(self):
    """Returns a tuple containing this layer's sublayers; may be empty."""
    return self._sublayers

  @property
  def weights(self):
    """Returns this layer's weights.

    Depending on the layer, the weights can be in the form of:

      - an empty tuple
      - a tensor (ndarray)
      - a nested structure of tuples and tensors

    TODO(jonni): Simplify this picture (and underlying implementation).
    """
    return self._weights

  @weights.setter
  def weights(self, weights):
    self._weights = weights

  @property
  def state(self):
    """Returns a tuple containing this layer's state; may be empty."""
    return self._state

  @state.setter
  def state(self, state):
    self._state = state

  @property
  def rng(self):
    """Returns a single-use random number generator without advancing it."""
    # TODO(lukaszkaiser, jonni): be even more explicit that we're not advancing.
    if isinstance(self._rng, int):
      self._rng = math.random.get_prng(self._rng)
    return self._rng

  @rng.setter
  def rng(self, rng):
    """Sets the rng (JAX PRNG key) for this layer and sublayers, recursively."""
    self._rng = rng
    sublayers = self.sublayers
    if sublayers:
      rngs = math.random.split(rng, len(sublayers))
      for sublayer, rng in zip(sublayers, rngs):
        sublayer.rng = rng

  def _clear_init_cache(self):
    self._init_cached = False
    for sublayer in self.sublayers:
      sublayer._clear_init_cache()  # pylint: disable=protected-access

  def pure_fn(self, x, weights, state, rng, use_cache=False):
    """Applies this layer as a pure function with no optional args.

    This method exposes the layer's computation as a pure function. This is
    especially useful for JIT compilation. Do not override, use `forward`
    instead.

    Args:
      x: Zero or more input tensors, packaged as described in the `Layer` class
          docstring.
      weights: A tuple or list of trainable weights, with one element for this
          layer if this layer has no sublayers, or one for each sublayer if
          this layer has sublayers. If a layer (or sublayer) has no trainable
          weights, the corresponding weights element is an empty tuple.
      state: Layer-specific non-parameter state that can update between batches.
      rng: Single-use random number generator (JAX PRNG key).
      use_cache: if `True`, cache weights and state in the layer object; used
        to implement layer sharing in combinators.

    Returns:
      A tuple of `(tensors, state)`. The tensors match the number (`n_out`)
      promised by this layer, and are packaged as described in the `Layer`
      class docstring.
    """
    try:
      old_weights, old_state, old_rng = self.weights, self.state, self.rng
      self._rng = rng
      if weights is GET_WEIGHTS_FROM_CACHE and state is GET_STATE_FROM_CACHE:  # pylint: disable=literal-comparison
        weights = self._weights
        state = self._state
      else:
        # In this case, we're called for the first time: cache weights.
        self._weights, self._state = weights, state

      if not self.has_backward:
        outputs = self.forward(x, weights)
        s = self.state
      else:
        outputs, s = self._do_custom_gradients(x, weights, state, rng=rng)
        self._state = s
      self._rng = old_rng
      if not use_cache:
        self.weights, self.state = old_weights, old_state
      return outputs, s

    except Exception as e:
      name, trace = self._name, _short_traceback(skip=1)
      raise LayerError(name, 'pure_fn',
                       self._caller, signature(x), trace) from e

  def output_signature(self, input_signature):
    """Returns output signature this layer would give for `input_signature`."""
    return self._forward_abstract(input_signature)[0]  # output only, not state

  def _forward_abstract(self, input_signature):
    """Computes shapes and dtypes this layer would produce in a forward pass.

    Args:
      input_signature: `ShapeDtype` instance (if this layer takes one input)
          or list/tuple of `ShapeDtype` instances.

    Returns:
      Tuple of (output, state).

      The output part of the tuple is a `ShapeDtype` instance representing the
      shape and type of the output (if this layer has one output) or a tuple
      of `ShapeDtype` instances (if this layer has more than one output).
    """
    try:
      # Note: By using rng_signature in place of an rng, we avoid computing and
      # permanently storing in global memory a large number of dropout masks.
      # TODO(jonni): Check if using an rng still carries this cost.
      dummy_rng = math.random.get_prng(0)
      rng_signature = ShapeDtype(dummy_rng.shape, dummy_rng.dtype)
      weight_signature = nested_map(signature, self.weights)
      forward_infer_shapes = math.abstract_eval(self.pure_fn)
      return forward_infer_shapes(
          input_signature, weight_signature, self.state, rng_signature)
    except Exception as e:
      name, trace = self._name, _short_traceback(skip=3)
      raise LayerError(name, '_forward_abstract', self._caller, input_signature,
                       trace) from e

  # pylint: disable=protected-access
  def _do_custom_gradients(self, x, weights, state, rng):
    """Calls this layer for a forward pass, but with custom gradients."""

    def _do_forward(y, weights):
      old_weights, old_state, old_rng = self._weights, self._state, self._rng
      res = self.forward(y, weights)
      s = self._state
      self._weights, self._state, self._rng = old_weights, old_state, old_rng
      return res, s

    def do_forward_vjp(y, weights):
      """Custom gradient (vjp) function."""
      old_weights, old_state, old_rng = self._weights, self._state, self._rng
      output = self.forward(y, weights)
      new_state = self._state
      self._weights, self._state, self._rng = old_weights, old_state, old_rng
      def vjpfun(grad):
        grad = grad[0]  # Ignore dummy gradient wrt state.
        res = self.backward(y, output, grad, weights, state, new_state, rng)
        return res
      return (output, new_state), vjpfun

    do_forward = math.custom_grad(do_forward_vjp, _do_forward)

    output, state = do_forward(x, weights)
    # TODO(lukaszkaiser): Investigate why we need this stop_gradient
    state = math.stop_gradient(state)
    return output, state


def layer(n_in=1, n_out=1, name=None):
  """Decorator for creating simple layers.  DEPRECATED; use base.Fn instead."""

  def _build_layer_class(raw_fn):
    """Returns a layer class whose callable instances execute the function."""

    def _init(self, **kwargs):
      self._kwargs = kwargs  # pylint: disable=protected-access
      Layer.__init__(self, n_in=n_in, n_out=n_out, name=name)

    def _forward(self, inputs, weights):
      """Uses this layer as part of a forward pass through the model."""
      del weights
      _validate_forward_input(inputs, n_in)
      raw_output = raw_fn(inputs, **self._kwargs)  # pylint: disable=protected-access
      output = () if _is_empty(raw_output) else raw_output
      return output

    # Set docstrings and create the class.
    _forward.__doc__ = raw_fn.__doc__
    # Note: None.__doc__ is None
    cls = type(raw_fn.__name__, (Layer,),
               {'__init__': _init,
                'forward': _forward})
    return cls

  return _build_layer_class


class PureLayer(Layer):
  """Pure function from inputs to outputs, packaged as neural network layer.

  The `PureLayer` class represents the simplest kinds of layers: layers with
  no trainable weights and no randomness, hence pure functions from inputs to
  outputs.
  """

  def __init__(self, forward_fn, n_in=1, n_out=1, name='PureLayer'):
    """Creates an unconnected `PureLayer` instance.

    Args:
      forward_fn: Pure function from input tensors to output tensors, where
          inputs and outputs are packaged as specified for `forward`.
      n_in: Number of inputs expected by this layer.
      n_out: Number of outputs promised by this layer.
      name: Class-like name for this layer; for use only in debugging.
    """
    super().__init__(n_in, n_out, name)
    self._forward_fn = forward_fn

  def forward(self, inputs, weights):
    """Overrides `Layer.forward`.

    Args:
      inputs: Zero or more input tensors, packaged as described in the `Layer`
          class docstring.
      weights: Trainable weights in general, but this subclass doesn't use
          weights, so the only acceptable value is an empty tuple/list.

    Returns:
      Zero or more output tensors, packaged as described in the `Layer` class
      docstring.

    Raises:
      ValueError: If weights is other than an empty tuple/list.
    """
    _validate_forward_input(inputs, self.n_in)
    raw_output = self._forward_fn(inputs)
    output = () if _is_empty(raw_output) else raw_output
    return output


def Fn(name, f, n_out=1):  # pylint: disable=invalid-name
  """Returns a layer with no weights that applies the function `f`.

  `f` can take and return any number of arguments, and takes only positional
  arguments -- no default or keyword arguments. It often uses JAX-numpy (`jnp`).
  The following, for example, would create a layer that takes two inputs and
  returns two outputs -- element-wise sums and maxima:

      `Fn('SumAndMax', lambda x0, x1: (x0 + x1, jnp.maximum(x0, x1)), n_out=2)`

  The layer's number of inputs (`n_in`) is automatically set to number of
  positional arguments in `f`, but you must explicitly set the number of
  outputs (`n_out`) whenever it's not the default value 1.

  Args:
    name: Class-like name for the resulting layer; for use in debugging.
    f: Pure function from input tensors to output tensors, where each input
        tensor is a separate positional arg, e.g., `f(x0, x1) --> x0 + x1`.
        Output tensors must be packaged as specified in the `Layer` class
        docstring.
    n_out: Number of outputs promised by the layer; default value 1.

  Returns:
    Layer executing the function `f`.
  """
  # Inspect the function f to restrict to no-defaults and no-kwargs functions.
  argspec = inspect.getfullargspec(f)
  if argspec.defaults is not None:
    raise ValueError('Function has default arguments (not allowed).')
  if argspec.varkw is not None:
    raise ValueError('Function has keyword arguments (not allowed).')
  if argspec.varargs is not None:
    raise ValueError('Function has variable args (not allowed).')

  def _forward(xs):  # pylint: disable=invalid-name
    if not isinstance(xs, (tuple, list)):
      xs = (xs,)
    return f(*xs)

  n_in = len(argspec.args)
  name = name or 'Fn'
  return PureLayer(_forward, n_in=n_in, n_out=n_out, name=name)


class LayerError(Exception):
  """Exception raised in the layer stack."""

  def __init__(self, layer_name, function_name, caller,
               input_signature, traceback_string):
    self._layer_name = layer_name
    self._function_name = function_name
    self._caller = caller  # Python inspect object with init caller info.
    self._traceback = traceback_string
    self._input_signature = input_signature
    super(LayerError, self).__init__(self.message)

  @property
  def message(self):
    """Assembles current layer context into an error message."""
    prefix = 'Exception passing through layer '
    prefix += '%s (in %s):\n' % (self._layer_name, self._function_name)
    short_path = '[...]/' + '/'.join(
        self._caller['filename'].split('/')[-3:])
    caller = '  layer created in file %s, line %d\n' % (short_path,
                                                        self._caller['lineno'])
    shapes_str = '  layer input shapes: %s\n\n' % str(self._input_signature)
    return prefix + caller + shapes_str + self._traceback


def to_list(outputs):
  """Converts layer outputs to a nested list, for easier equality testing.

  Args:
    outputs: A tensor or tuple/list of tensors coming from the forward
        application of a layer. Each tensor is NumPy ndarray-like, which
        complicates simple equality testing (e.g., via `assertEquals`):
        such tensors require equality testing to use either `all` (all
        elements match) or `any` (at least one element matches), which is not
        directly supported in `absltest`.

  Returns:
    A nested list structure containing all the output values, but now directly
    testable using `assertEquals`.
  """
  if isinstance(outputs, (list, tuple)):
    return [y.tolist() for y in outputs]
  else:
    return outputs.tolist()


def _validate_forward_input(x, n_in):
  if n_in != 1:
    if not isinstance(x, (tuple, list)):
      raise TypeError(
          f'Expected input to be a tuple or list; instead got {type(x)}.')
    if len(x) != n_in:
      raise ValueError(f'Input tuple length ({len(x)}) does not equal required '
                       f'number of inputs ({n_in}).')


def _is_empty(container):
  if container is None:
    raise ValueError('Argument "container" is None.')
  return isinstance(container, (list, tuple)) and len(container) == 0  # pylint: disable=g-explicit-length-test


def _find_frame(frame):
  """Find the frame with the caller on the stack."""
  # TODO(lukaszkaiser): rewrite this function in a systematic way.
  # We want to find the first place where the layer was called
  # that is *not* an __init__ function of an inheriting layer.
  # We also need to exclude a few decorator functions.
  while frame.f_code.co_name in ['__init__', 'gin_wrapper', '_validate',
                                 '_validate_forward_inputs', '_init']:
    # We only skip __init__ in internal layers, return otherwise.
    try:
      dirname = frame.f_code.co_filename.split('/')[-2]
    except IndexError:
      # Notebook cells have dummy filenames that do not contain any slashes
      dirname = frame.f_code.co_filename
    if dirname != 'layers' and frame.f_code.co_name == '__init__':
      return frame
    # If we are in an init, move up.
    frame = frame.f_back
  return frame


def _shorten_file_path(line):
  """Shorten file path in error lines for more readable tracebacks."""
  start = line.lower().find('file')
  if start < 0:
    return line
  first_quote = line.find('"', start)
  if first_quote < 0:
    return line
  second_quote = line.find('"', first_quote + 1)
  if second_quote < 0:
    return line
  path = line[first_quote + 1:second_quote]
  new_path = '/'.join(path.split('/')[-3:])
  return line[:first_quote] + '[...]/' + new_path + line[second_quote + 1:]


def _short_traceback(skip=3):
  """Cleaned-up form of traceback."""
  counter, res = 0, []
  # Skipping 3 lines by default: the top (useless) and self-call.
  # In python 3, we need to set chain to False (it doesn't exist in python 2).
  lines = traceback.format_exc(chain=False).splitlines()[skip:]  # pylint: disable=unexpected-keyword-arg
  for l in lines:
    if l.startswith('trax.layers.base.LayerError'):
      l = l[len('trax.layers.base.'):]  # Remove the trax.layers.base prefix.
    res.append(_shorten_file_path(l))
    if counter % 2 == 1:
      res.append('')
    counter += 1
    # If we see a LayerError, the traceback has already been processed.
    if l.startswith('LayerError'):
      # Skip 4 back except last as these are internal base-layer calls.
      res = res[:-4] + [res[-1]]
      res += lines[counter:]
      break
  return '\n'.join(res)


def _random_values(input_signature, rng):
  """Creates random floats or ints of the given shape.

  Args:
    input_signature: A `ShapeDtype` instance (if `layer_obj` takes one input)
        or a list/tuple of ShapeDtype instances.
    rng: Single-use random number generator (JAX PRNG key).

  Returns:
    Random values with the shape and type specified.
  """
  if isinstance(input_signature, ShapeDtype):
    shape, dtype = input_signature.shape, input_signature.dtype
    if np.issubdtype(dtype, np.integer):
      return math.random.bernoulli(rng, 0.5, shape).astype(np.int32)
    else:
      return math.random.uniform(rng, shape, minval=-1.0, maxval=1.0)
  elif isinstance(input_signature, (list, tuple)):
    return tuple(_random_values(x, rng) for x in input_signature)
  else:
    raise TypeError(type(input_signature))


def _shapes(x):
  """Gets a structure of shapes for a structure of nested arrays."""
  def shape(x):
    try:
      return tuple([int(i) for i in x.shape])
    except Exception:  # pylint: disable=broad-except
      return ()
  return tuple(nested_map(shape, x))


def jit_forward(forward, n_devices, do_mean=True):
  """Returns a JIT-compiled forward function running on `n_devices`."""
  model_predict = _accelerate(forward, n_devices)
  if n_devices == 1:
    return model_predict

  def predict(x, weights, state, rng):
    """Predict function JIT-compileds and parallelized as requested."""
    res, state = _combine_devices(model_predict(
        reshape_by_device(x, n_devices),
        weights,
        state,
        jnp.stack(math.random.split(rng, n_devices))))
    if do_mean:
      return math.nested_map(lambda y: jnp.mean(y, axis=0), res), state
    else:
      return res, state

  return predict


def _combine_devices(x_tuple):
  """Combines multi-device tensors into a single batch."""
  def f(x):
    if len(x.shape) < 2:
      return x  # No extra batch dimension: use devices as batch, so return.
    batch_size = x.shape[0] * x.shape[1]
    return math.numpy.reshape(x, [batch_size] + list(x.shape[2:]))
  return math.nested_map(f, x_tuple)


def _accelerate(f, n_devices):
  """JIT-compiled version of `f` running on `n_devices`."""
  if n_devices == 1:
    return math.jit(f)

  return math.pmap(f, axis_name='batch')


def reshape_by_device(x, n_devices):
  """Reshapes possibly nested `x` into a shape `(n_devices, ...)`."""
  def f(x):
    x_shape = list(x.shape)
    batch_size = x_shape[0]
    batch_size_per_device = batch_size // n_devices
    if batch_size_per_device * n_devices != batch_size:
      raise ValueError(f'Number of devices ({n_devices}) does not evenly '
                       f'divide batch size ({batch_size}).')
    new_shape_prefix = [n_devices, batch_size_per_device]
    return math.numpy.reshape(x, new_shape_prefix + x_shape[1:])
  return math.nested_map(f, x)


def for_n_devices(x, n_devices):
  """Replicates/broadcasts `x` for `n_devices`."""
  def f(x):
    if n_devices > 1 and math.backend_name() == 'jax':
      return _multi_device_put(x)
    elif n_devices > 1:
      return jnp.broadcast_to(x, (n_devices,) + x.shape)
    else:
      return x
  return math.nested_map(f, x)


def _multi_device_put(x, devices=None):
  """Memory efficient multi-device replication / broadcast in JAX.

  JAX uses a ShardedDeviceArray class that holds a list of device buffers
  on separate devices for use with pmap'd computations.  Sharded arrays
  are explicitly used to eliminate unnecessary inter-device transfer of
  memory buffers between use in pmap'd computations.  The JAX API currently
  does not have a multi-device 'put' function that copies a buffer onto
  N devices in a memory-efficient fashion, so we implement our own here.

  Args:
    x: jax DeviceArray or numpy ndarray to be replicated.
    devices: a jax.devices() list or subset thereof of devices to
      replicate onto.  Should match the list passed to any pmaps
      ingesting the replicated array.

  Returns:
    A ShardedDeviceArray with
    dtype = x.dtype and shape = (n_devices,) + x.shape
    that's backed by replicated device_buffers on each local device.
  """
  # Convert _FilledConstants that don't have device_buffer, etc.
  if type(x) != jax.xla.DeviceArray:  # pylint: disable=unidiomatic-typecheck
    x = jnp.array(x)
  # Calculate the abstract shape of the replicated array.
  if not devices:
    devices = jax.local_devices()
  n_devices = len(devices)
  x_aval = jax.xla.abstractify(x)
  broadcast_x_aval = jax.abstract_arrays.ShapedArray(
      (n_devices,) + x_aval.shape,
      x_aval.dtype)
  # Create copies of the underlying device buffer for each local device.
  broadcast_buffers = [
      jax.interpreters.xla.device_put(x, dv)
      for dv in devices
  ]
  return jax.pxla.ShardedDeviceArray(broadcast_x_aval, broadcast_buffers)
