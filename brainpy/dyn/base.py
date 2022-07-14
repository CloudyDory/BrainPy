# -*- coding: utf-8 -*-

import gc
import inspect
from typing import Union, Dict, Callable, Sequence, Optional, Tuple, Any

import jax.numpy as jnp
import numpy as np

import brainpy.math as bm
from brainpy import tools
from brainpy.algorithms import OnlineAlgorithm, OfflineAlgorithm
from brainpy.base.base import Base
from brainpy.base.collector import Collector
from brainpy.connect import TwoEndConnector, MatConn, IJConn, One2One, All2All
from brainpy.errors import ModelBuildError, NoImplementationError
from brainpy.initialize import Initializer, parameter, variable, Uniform, noise as init_noise
from brainpy.integrators import odeint, sdeint
from brainpy.modes import Mode, TrainingMode, BatchingMode, normal, training
from brainpy.tools.others import to_size, size2num
from brainpy.types import Tensor, Shape

__all__ = [
  # general class
  'DynamicalSystem',

  # containers
  'Container', 'Network', 'Sequential',

  # channel models
  'Channel',

  # neuron models
  'NeuGroup', 'CondNeuGroup',

  # synapse models
  'SynConn', 'SynOutput', 'SynSTP', 'SynLTP', 'TwoEndConn',
]


def not_customized(fun: Callable) -> Callable:
  """Marks the given module method is not implemented.

  Methods wrapped in @not_customized can define submodules directly within the method.

  For instance::

    @not_customized
    init_fb(self):
      ...

    @not_customized
    def feedback(self):
      ...
  """
  fun.not_implemented = True
  return fun


class DynamicalSystem(Base):
  """Base Dynamical System class.

  Parameters
  ----------
  name : optional, str
    The name of the dynamical system.
  mode: Mode
    The model computation mode. It should be instance of :py:class:`~.Mode`.
  """

  '''Global delay data, which stores the delay variables and corresponding delay targets. 
   
  This variable is useful when the same target variable is used in multiple mappings, 
  as it can reduce the duplicate delay variable registration.'''
  global_delay_data: Dict[str, Tuple[Union[bm.LengthDelay, None], bm.Variable]] = dict()

  '''Online fitting method.'''
  online_fit_by: Optional[OnlineAlgorithm]

  '''Offline fitting method.'''
  offline_fit_by: Optional[OfflineAlgorithm]

  def __init__(
      self,
      name: str = None,
      mode: Mode = normal,
  ):
    super(DynamicalSystem, self).__init__(name=name)

    # local delay variables
    self.local_delay_vars: Dict[str, bm.LengthDelay] = Collector()

    # mode setting
    if not isinstance(mode, Mode):
      raise ValueError(f'Should be instance of {Mode.__name__}, but we got {type(Mode)}: {Mode}')
    self._mode = mode

    # fitting parameters
    self.online_fit_by = None
    self.offline_fit_by = None
    self.fit_record = dict()

  @property
  def mode(self) -> Mode:
    return self._mode

  @mode.setter
  def mode(self, value):
    if not isinstance(value, Mode):
      raise ValueError(f'Must be instance of {Mode.__name__}, but we got {type(value)}: {value}')
    self._mode = value

  def __repr__(self):
    return f'{self.__class__.__name__}(name={self.name}, mode={self.mode})'

  def __call__(self, *args, **kwargs):
    """The shortcut to call ``update`` methods."""
    return self.update(*args, **kwargs)

  def register_delay(
      self,
      identifier: str,
      delay_step: Optional[Union[int, Tensor, Callable, Initializer]],
      delay_target: bm.Variable,
      initial_delay_data: Union[Initializer, Callable, Tensor, float, int, bool] = None,
  ):
    """Register delay variable.

    Parameters
    ----------
    identifier: str
      The delay variable name.
    delay_step: Optional, int, JaxArray, ndarray, callable, Initializer
      The number of the steps of the delay.
    delay_target: Variable
      The target variable for delay.
    initial_delay_data: float, int, JaxArray, ndarray, callable, Initializer
      The initializer for the delay data.

    Returns
    -------
    delay_step: int, JaxArray, ndarray
      The number of the delay steps.
    """
    # delay steps
    if delay_step is None:
      delay_type = 'none'
    elif isinstance(delay_step, (int, np.integer, jnp.integer)):
      delay_type = 'homo'
    elif isinstance(delay_step, (bm.ndarray, jnp.ndarray, np.ndarray)):
      if delay_step.size == 1 and delay_step.ndim == 0:
        delay_type = 'homo'
      else:
        delay_type = 'heter'
        delay_step = bm.asarray(delay_step)
    elif callable(delay_step):
      delay_step = parameter(delay_step, delay_target.shape, allow_none=False)
      delay_type = 'heter'
    else:
      raise ValueError(f'Unknown "delay_steps" type {type(delay_step)}, only support '
                       f'integer, array of integers, callable function, brainpy.init.Initializer.')
    if delay_type == 'heter':
      if delay_step.dtype not in [bm.int32, bm.int64]:
        raise ValueError('Only support delay steps of int32, int64. If your '
                         'provide delay time length, please divide the "dt" '
                         'then provide us the number of delay steps.')
      if delay_target.shape[0] != delay_step.shape[0]:
        raise ValueError(f'Shape is mismatched: {delay_target.shape[0]} != {delay_step.shape[0]}')
    if delay_type != 'none':
      max_delay_step = int(bm.max(delay_step))

    # delay target
    if delay_type != 'none':
      if not isinstance(delay_target, bm.Variable):
        raise ValueError(f'"delay_target" must be an instance of Variable, but we got {type(delay_target)}')

    # delay variable
    if delay_type != 'none':
      if identifier not in self.global_delay_data:
        delay = bm.LengthDelay(delay_target, max_delay_step, initial_delay_data)
        self.global_delay_data[identifier] = (delay, delay_target)
        self.local_delay_vars[identifier] = delay
      else:
        delay = self.global_delay_data[identifier][0]
        if delay is None:
          delay = bm.LengthDelay(delay_target, max_delay_step, initial_delay_data)
          self.global_delay_data[identifier] = (delay, delay_target)
          self.local_delay_vars[identifier] = delay
        elif delay.num_delay_step - 1 < max_delay_step:
          self.global_delay_data[identifier][0].reset(delay_target, max_delay_step, initial_delay_data)
    else:
      self.global_delay_data[identifier] = (None, delay_target)
    self.register_implicit_nodes(self.local_delay_vars)
    return delay_step

  def get_delay_data(
      self,
      identifier: str,
      delay_step: Optional[Union[int, bm.JaxArray, jnp.DeviceArray]],
      *indices: Union[int, slice, bm.JaxArray, jnp.DeviceArray],
  ):
    """Get delay data according to the provided delay steps.

    Parameters
    ----------
    identifier: str
      The delay variable name.
    delay_step: Optional, int, JaxArray, ndarray
      The delay length.
    indices: optional, int, slice, JaxArray, ndarray
      The indices of the delay.

    Returns
    -------
    delay_data: JaxArray, ndarray
      The delay data at the given time.
    """
    if delay_step is None:
      return self.global_delay_data[identifier][1].value

    if identifier in self.global_delay_data:
      if bm.ndim(delay_step) == 0:
        return self.global_delay_data[identifier][0](delay_step, *indices)
      else:
        if len(indices) == 0:
          indices = (jnp.arange(delay_step.size),)
        return self.global_delay_data[identifier][0](delay_step, *indices)

    elif identifier in self.local_delay_vars:
      if bm.ndim(delay_step) == 0:
        return self.local_delay_vars[identifier](delay_step)
      else:
        if len(indices) == 0:
          indices = (jnp.arange(delay_step.size),)
        return self.local_delay_vars[identifier](delay_step, *indices)

    else:
      raise ValueError(f'{identifier} is not defined in delay variables.')

  def update(self, *args, **kwargs):
    """The function to specify the updating rule.

    Assume any dynamical system depends on the shared variables (`sha`),
    like time variable ``t``, the step precision ``dt``, and the time step `i`.
    """
    raise NotImplementedError('Must implement "update" function by subclass self.')

  def reset(self, batch_size=None):
    """Reset function which reset the whole variables in the model.
    """
    self.reset_state(batch_size)

  def reset_state(self, batch_size=None):
    """Reset function which reset the states in the model.
    """
    child_nodes = self.nodes(level=1, include_self=False).subset(DynamicalSystem).unique()
    if len(child_nodes) > 0:
      for node in child_nodes.values():
        node.reset_state(batch_size=batch_size)
    else:
      raise NotImplementedError('Must implement "reset_state" function by subclass self. '
                                f'Error of {self.name}')

  def update_local_delays(self, nodes: Union[Sequence, Dict] = None):
    """Update local delay variables.

    Parameters
    ----------
    nodes: sequence, dict
      The nodes to update their delay variables.
    """
    # update delays
    if nodes is None:
      nodes = self.nodes(level=1, include_self=False).subset(DynamicalSystem).unique().values()
    elif isinstance(nodes, dict):
      nodes = nodes.values()
    for node in nodes:
      for name in node.local_delay_vars:
        delay = self.global_delay_data[name][0]
        target = self.global_delay_data[name][1]
        delay.update(target.value)

  def reset_local_delays(self, nodes: Union[Sequence, Dict] = None):
    """Reset local delay variables.

    Parameters
    ----------
    nodes: sequence, dict
      The nodes to Reset their delay variables.
    """
    # reset delays
    if nodes is None:
      nodes = self.nodes(level=1, include_self=False).subset(DynamicalSystem).unique().values()
    elif isinstance(nodes, dict):
      nodes = nodes.values()
    for node in nodes:
      for name in node.local_delay_vars:
        delay = self.global_delay_data[name][0]
        target = self.global_delay_data[name][1]
        delay.reset(target.value)

  def __del__(self):
    """Function for handling `del` behavior.

    This function is used to pop out the variables which registered in global delay data.
    """
    if hasattr(self, 'local_delay_vars'):
      for key in tuple(self.local_delay_vars.keys()):
        val = self.global_delay_data.pop(key)
        del val
        val = self.local_delay_vars.pop(key)
        del val
    if hasattr(self, 'implicit_nodes'):
      for key in tuple(self.implicit_nodes.keys()):
        del self.implicit_nodes[key]
    if hasattr(self, 'implicit_vars'):
      for key in tuple(self.implicit_vars.keys()):
        del self.implicit_vars[key]
    for key in tuple(self.__dict__.keys()):
      del self.__dict__[key]
    gc.collect()

  @not_customized
  def online_init(self):
    raise NoImplementationError('Subclass must implement online_init() function when using OnlineTrainer.')

  @not_customized
  def offline_init(self):
    raise NoImplementationError('Subclass must implement offline_init() function when using OfflineTrainer.')

  @not_customized
  def online_fit(self,
                 target: Tensor,
                 fit_record: Dict[str, Tensor]):
    raise NoImplementationError('Subclass must implement online_fit() function when using OnlineTrainer.')

  @not_customized
  def offline_fit(self,
                  target: Tensor,
                  fit_record: Dict[str, Tensor]):
    raise NoImplementationError('Subclass must implement offline_fit() function when using OfflineTrainer.')


class Container(DynamicalSystem):
  """Container object which is designed to add other instances of DynamicalSystem.

  Parameters
  ----------
  steps : tuple of function, tuple of str, dict of (str, function), optional
      The step functions.
  monitors : tuple, list, Monitor, optional
      The monitor object.
  name : str, optional
      The object name.
  show_code : bool
      Whether show the formatted code.
  ds_dict : dict of (str, )
      The instance of DynamicalSystem with the format of "key=dynamic_system".
  """

  def __init__(
      self,
      *ds_tuple,
      name: str = None,
      mode: Mode = normal,
      **ds_dict
  ):
    super(Container, self).__init__(name=name, mode=mode)

    # # children dynamical systems
    # self.implicit_nodes = Collector()
    # for ds in ds_tuple:
    #   if not isinstance(ds, DynamicalSystem):
    #     raise ModelBuildError(f'{self.__class__.__name__} receives instances of '
    #                           f'DynamicalSystem, however, we got {type(ds)}.')
    #   if ds.name in self.implicit_nodes:
    #     raise ValueError(f'{ds.name} has been paired with {ds}. Please change a unique name.')
    # self.register_implicit_nodes({node.name: node for node in ds_tuple})
    # for key, ds in ds_dict.items():
    #   if not isinstance(ds, DynamicalSystem):
    #     raise ModelBuildError(f'{self.__class__.__name__} receives instances of '
    #                           f'DynamicalSystem, however, we got {type(ds)}.')
    #   if key in self.implicit_nodes:
    #     raise ValueError(f'{key} has been paired with {ds}. Please change a unique name.')
    # self.register_implicit_nodes(ds_dict)

    # add tuple-typed components
    for module in ds_tuple:
      if isinstance(module, DynamicalSystem):
        self.implicit_nodes[module.name] = module
      elif isinstance(module, (list, tuple)):
        for m in module:
          if not isinstance(m, DynamicalSystem):
            raise ValueError(f'Should be instance of {DynamicalSystem.__name__}. '
                             f'But we got {type(m)}')
          self.implicit_nodes[m.name] = module
      elif isinstance(module, dict):
        for k, v in module.items():
          if not isinstance(v, DynamicalSystem):
            raise ValueError(f'Should be instance of {DynamicalSystem.__name__}. '
                             f'But we got {type(v)}')
          self.implicit_nodes[k] = v
      else:
        raise ValueError(f'Cannot parse sub-systems. They should be {DynamicalSystem.__name__} '
                         f'or a list/tuple/dict of  {DynamicalSystem.__name__}.')
    # add dict-typed components
    for k, v in ds_dict.items():
      if not isinstance(v, DynamicalSystem):
        raise ValueError(f'Should be instance of {DynamicalSystem.__name__}. '
                         f'But we got {type(v)}')
      self.implicit_nodes[k] = v

  def __repr__(self):
    cls_name = self.__class__.__name__
    split = ', '
    children = [f'{key}={str(val)}' for key, val in self.implicit_nodes.items()]
    return f'{cls_name}({split.join(children)})'

  def update(self, tdi, *args, **kwargs):
    """Update function of a container.

    In this update function, the update functions in children systems are
    iteratively called.
    """
    nodes = self.nodes(level=1, include_self=False).subset(DynamicalSystem).unique()
    for node in nodes.values():
      node.update(tdi)

  def __getitem__(self, item):
    """Wrap the slice access (self['']). """
    if item in self.implicit_nodes:
      return self.implicit_nodes[item]
    else:
      raise ValueError(f'Unknown item {item}, we only found {list(self.implicit_nodes.keys())}')

  def __getattr__(self, item):
    """Wrap the dot access ('self.'). """
    child_ds = super(Container, self).__getattribute__('implicit_nodes')
    if item in child_ds:
      return child_ds[item]
    else:
      return super(Container, self).__getattribute__(item)


class Network(Container):
  """Base class to model network objects, an alias of Container.

  Network instantiates a network, which is aimed to load
  neurons, synapses, and other brain objects.

  Parameters
  ----------
  name : str, Optional
    The network name.
  monitors : optional, list of str, tuple of str
    The items to monitor.
  ds_tuple :
    A list/tuple container of dynamical system.
  ds_dict :
    A dict container of dynamical system.
  """

  def __init__(
      self,
      *ds_tuple,
      name: str = None,
      mode: Mode = normal,
      **ds_dict
  ):
    super(Network, self).__init__(*ds_tuple,
                                  name=name,
                                  mode=mode,
                                  **ds_dict)

  def update(self, *args, **kwargs):
    """Step function of a network.

    In this update function, the update functions in children systems are
    iteratively called.
    """
    nodes = self.nodes(level=1, include_self=False)
    nodes = nodes.subset(DynamicalSystem)
    nodes = nodes.unique()
    neuron_groups = nodes.subset(NeuGroup)
    synapse_groups = nodes.subset(SynConn)
    other_nodes = nodes - neuron_groups - synapse_groups

    # shared arguments
    shared = args[0]

    # update synapse nodes
    for node in synapse_groups.values():
      node.update(shared)

    # update neuron nodes
    for node in neuron_groups.values():
      node.update(shared)

    # update other types of nodes
    for node in other_nodes.values():
      node.update(shared)

    # update delays
    self.update_local_delays(nodes)

  def reset_state(self, batch_size=None):
    nodes = self.nodes(level=1, include_self=False).subset(DynamicalSystem).unique()
    neuron_groups = nodes.subset(NeuGroup)
    synapse_groups = nodes.subset(SynConn)

    # reset neuron nodes
    for node in neuron_groups.values():
      node.reset_state(batch_size)

    # reset synapse nodes
    for node in synapse_groups.values():
      node.reset_state(batch_size)

    # reset other types of nodes
    for node in (nodes - neuron_groups - synapse_groups).values():
      node.reset_state(batch_size)

    # reset delays
    self.reset_local_delays(nodes)


class NeuGroup(DynamicalSystem):
  """Base class to model neuronal groups.

  There are several essential attributes:

  - ``size``: the geometry of the neuron group. For example, `(10, )` denotes a line of
    neurons, `(10, 10)` denotes a neuron group aligned in a 2D space, `(10, 15, 4)` denotes
    a 3-dimensional neuron group.
  - ``num``: the flattened number of neurons in the group. For example, `size=(10, )` => \
    `num=10`, `size=(10, 10)` => `num=100`, `size=(10, 15, 4)` => `num=600`.

  Parameters
  ----------
  size : int, tuple of int, list of int
    The neuron group geometry.
  name : optional, str
    The name of the dynamic system.
  keep_size: bool
    Whether keep the geometry information.

    .. versionadded:: 2.1.13
  """

  def __init__(
      self,
      size: Shape,
      name: str = None,
      keep_size: bool = False,
      mode: Mode = normal,
  ):
    # size
    if isinstance(size, (list, tuple)):
      if len(size) <= 0:
        raise ModelBuildError(f'size must be int, or a tuple/list of int. '
                              f'But we got {type(size)}')
      if not isinstance(size[0], int):
        raise ModelBuildError('size must be int, or a tuple/list of int.'
                              f'But we got {type(size)}')
      size = tuple(size)
    elif isinstance(size, int):
      size = (size,)
    else:
      raise ModelBuildError('size must be int, or a tuple/list of int.'
                            f'But we got {type(size)}')
    self.size = size
    self.keep_size = keep_size
    # number of neurons
    self.num = tools.size2num(size)

    # initialize
    super(NeuGroup, self).__init__(name=name, mode=mode)

  @property
  def varshape(self):
    return self.size if self.keep_size else (self.num,)

  def get_batch_shape(self, batch_size=None):
    if batch_size is None:
      return self.varshape
    else:
      return (batch_size,) + self.varshape

  def update(self, tdi, x=None):
    """The function to specify the updating rule.

    Parameters
    ----------
    tdi : DotDict
      The shared arguments, especially time `t`, step `dt`, and iteration `i`.
    x: Any
      The input for a neuron group.
    """
    raise NotImplementedError(f'Subclass of {self.__class__.__name__} must '
                              f'implement "update" function.')


class SynConn(DynamicalSystem):
  """Base class to model two-end synaptic connections.

  Parameters
  ----------
  pre : NeuGroup
    Pre-synaptic neuron group.
  post : NeuGroup
    Post-synaptic neuron group.
  conn : optional, ndarray, JaxArray, dict, TwoEndConnector
    The connection method between pre- and post-synaptic groups.
  name : str, optional
    The name of the dynamic system.
  """

  def __init__(
      self,
      pre: NeuGroup,
      post: NeuGroup,
      conn: Union[TwoEndConnector, Tensor, Dict[str, Tensor]] = None,
      name: str = None,
      mode: Mode = normal,
  ):
    super(SynConn, self).__init__(name=name, mode=mode)

    # pre or post neuron group
    # ------------------------
    if not isinstance(pre, NeuGroup):
      raise ModelBuildError('"pre" must be an instance of NeuGroup.')
    if not isinstance(post, NeuGroup):
      raise ModelBuildError('"post" must be an instance of NeuGroup.')
    self.pre = pre
    self.post = post

    # connectivity
    # ------------
    if isinstance(conn, TwoEndConnector):
      self.conn = conn(pre.size, post.size)
    elif isinstance(conn, (bm.ndarray, np.ndarray, jnp.ndarray)):
      if (pre.num, post.num) != conn.shape:
        raise ModelBuildError(f'"conn" is provided as a matrix, and it is expected '
                              f'to be an array with shape of (pre.num, post.num) = '
                              f'{(pre.num, post.num)}, however we got {conn.shape}')
      self.conn = MatConn(conn_mat=conn)
    elif isinstance(conn, dict):
      if not ('i' in conn and 'j' in conn):
        raise ModelBuildError(f'"conn" is provided as a dict, and it is expected to '
                              f'be a dictionary with "i" and "j" specification, '
                              f'however we got {conn}')
      self.conn = IJConn(i=conn['i'], j=conn['j'])
    elif isinstance(conn, str):
      self.conn = conn
    elif conn is None:
      self.conn = None
    else:
      raise ModelBuildError(f'Unknown "conn" type: {conn}')

  def check_pre_attrs(self, *attrs):
    """Check whether pre group satisfies the requirement."""
    if not hasattr(self, 'pre'):
      raise ModelBuildError('Please call __init__ function first.')
    for attr in attrs:
      if not isinstance(attr, str):
        raise ValueError(f'Must be string. But got {attr}.')
      if not hasattr(self.pre, attr):
        raise ModelBuildError(f'{self} need "pre" neuron group has attribute "{attr}".')

  def check_post_attrs(self, *attrs):
    """Check whether post group satisfies the requirement."""
    if not hasattr(self, 'post'):
      raise ModelBuildError('Please call __init__ function first.')
    for attr in attrs:
      if not isinstance(attr, str):
        raise ValueError(f'Must be string. But got {attr}.')
      if not hasattr(self.post, attr):
        raise ModelBuildError(f'{self} need "pre" neuron group has attribute "{attr}".')

  def update(self, tdi, pre_spike=None):
    """The function to specify the updating rule.

    Assume any dynamical system depends on the shared variables (`sha`),
    like time variable ``t``, the step precision ``dt``, and the time step `i`.
    """
    raise NotImplementedError('Must implement "update" function by subclass self.')


class SynComponent(DynamicalSystem):
  master: SynConn

  def reset_state(self, batch_size=None):
    pass

  def filter(self, g):
    return g

  def __call__(self, *args, **kwargs):
    return self.filter(*args, **kwargs)

  def register_master(self, master: SynConn):
    if not isinstance(master, SynConn):
      raise TypeError(f'master must be instance of {SynConn.__name__}, but we got {type(master)}')
    if hasattr(self, 'master') and self.master != master:
      raise ValueError(f'master has been registered, but we got another master going to be registered.')
    self.master = master

  def __repr__(self):
    return self.__class__.__name__


class SynOutput(SynComponent):
  """Base class for synaptic current output."""

  def update(self, tdi):
    pass


class SynSTP(SynComponent):
  """Base class for synaptic short-term plasticity."""

  def update(self, tdi, pre_spike):
    pass


class SynLTP(SynComponent):
  """Base class for synaptic long-term plasticity."""

  def update(self, tdi, pre_spike):
    pass


class TwoEndConn(SynConn):
  """Base class to model synaptic connections.

  Parameters
  ----------
  pre : NeuGroup
    Pre-synaptic neuron group.
  post : NeuGroup
    Post-synaptic neuron group.
  conn : optional, ndarray, JaxArray, dict, TwoEndConnector
    The connection method between pre- and post-synaptic groups.
  output: SynOutput
    The output for the synaptic current.

    .. versionadded:: 2.1.13
       The output component for a two-end connection model.

  stp: SynSTP
    The short-term plasticity model for the synaptic variables.

    .. versionadded:: 2.1.13
       The short-term plasticity component for a two-end connection model.

  ltp: SynLTP
    The long-term plasticity model for the synaptic variables.

    .. versionadded:: 2.1.13
       The long-term plasticity component for a two-end connection model.

  name : str, optional
    The name of the dynamic system.
  """

  def __init__(
      self,
      pre: NeuGroup,
      post: NeuGroup,
      conn: Union[TwoEndConnector, Tensor, Dict[str, Tensor]] = None,
      output: Optional[SynOutput] = None,
      stp: Optional[SynSTP] = None,
      ltp: Optional[SynLTP] = None,
      name: str = None,
      mode: Mode = normal,
  ):
    super(TwoEndConn, self).__init__(pre=pre,
                                     post=post,
                                     conn=conn,
                                     name=name,
                                     mode=mode)

    # synaptic output
    if output is None: output = SynOutput()
    if not isinstance(output, SynOutput):
      raise TypeError(f'output must be instance of {SynOutput.__name__}, but we got {type(output)}')
    self.output: SynOutput = output
    self.output.register_master(master=self)

    # short-term synaptic plasticity
    if stp is not None:
      if not isinstance(stp, SynSTP):
        raise TypeError(f'Short-term plasticity must be instance of {SynSTP.__name__}, '
                        f'but we got {type(stp)}')
      stp.register_master(master=self)
    self.stp: Optional[SynSTP] = stp

    # long-term synaptic plasticity
    if ltp is not None:
      if not isinstance(ltp, SynLTP):
        raise TypeError(f'Long-term plasticity must be instance of {SynLTP.__name__}, '
                        f'but we got {type(ltp)}')
      ltp.register_master(master=self)
    self.ltp: Optional[SynLTP] = ltp

  def init_weights(
      self,
      weight: Union[float, Tensor, Initializer, Callable],
      comp_method: str,
      sparse_data: str = 'csr'
  ) -> Union[float, Tensor]:
    if comp_method not in ['sparse', 'dense']:
      raise ValueError(f'"comp_method" must be in "sparse" and "dense", but we got {comp_method}')
    if sparse_data not in ['csr', 'ij']:
      raise ValueError(f'"sparse_data" must be in "csr" and "ij", but we got {sparse_data}')
    if self.conn is None:
      raise ValueError(f'Must provide "conn" when initialize the model {self.name}')

    # connections and weights
    if isinstance(self.conn, One2One):
      weight = parameter(weight, (self.pre.num,), allow_none=False)
      conn_mask = None

    elif isinstance(self.conn, All2All):
      weight = parameter(weight, (self.pre.num, self.post.num), allow_none=False)
      conn_mask = None

    else:
      if comp_method == 'sparse':
        if sparse_data == 'csr':
          conn_mask = self.conn.require('pre2post')
        elif sparse_data == 'ij':
          conn_mask = self.conn.require('post_ids', 'pre_ids')
        else:
          ValueError(f'Unknown sparse data type: {sparse_data}')
        weight = parameter(weight, conn_mask[1].shape, allow_none=False)
      elif comp_method == 'dense':
        weight = parameter(weight, (self.pre.num, self.post.num), allow_none=False)
        conn_mask = self.conn.require('conn_mat')
      else:
        raise ValueError(f'Unknown connection type: {comp_method}')

    # training weights
    if isinstance(self.mode, TrainingMode):
      weight = bm.TrainVar(weight)
    return weight, conn_mask

  def syn2post_with_all2all(self, syn_value, syn_weight):
    if bm.ndim(syn_weight) == 0:
      if isinstance(self.mode, BatchingMode):
        post_vs = bm.sum(syn_value, keepdims=True, axis=tuple(range(syn_value.ndim))[1:])
      else:
        post_vs = bm.sum(syn_value)
      if not self.conn.include_self:
        post_vs = post_vs - syn_value
      post_vs = syn_weight * post_vs
    else:
      post_vs = syn_value @ syn_weight
    return post_vs

  def syn2post_with_one2one(self, syn_value, syn_weight):
    return syn_value * syn_weight

  def syn2post_with_dense(self, syn_value, syn_weight, conn_mat):
    if bm.ndim(syn_weight) == 0:
      post_vs = (syn_weight * syn_value) @ conn_mat
    else:
      post_vs = syn_value @ (syn_weight * conn_mat)
    return post_vs


class CondNeuGroup(NeuGroup, Container):
  r"""Base class to model conductance-based neuron group.

  The standard formulation for a conductance-based model is given as

  .. math::

      C_m {dV \over dt} = \sum_jg_j(E - V) + I_{ext}

  where :math:`g_j=\bar{g}_{j} M^x N^y` is the channel conductance, :math:`E` is the
  reversal potential, :math:`M` is the activation variable, and :math:`N` is the
  inactivation variable.

  :math:`M` and :math:`N` have the dynamics of

  .. math::

      {dx \over dt} = \phi_x {x_\infty (V) - x \over \tau_x(V)}

  where :math:`x \in [M, N]`, :math:`\phi_x` is a temperature-dependent factor,
  :math:`x_\infty` is the steady state, and :math:`\tau_x` is the time constant.
  Equivalently, the above equation can be written as:

  .. math::

      \frac{d x}{d t}=\phi_{x}\left(\alpha_{x}(1-x)-\beta_{x} x\right)

  where :math:`\alpha_{x}` and :math:`\beta_{x}` are rate constants.

  .. versionadded:: 2.1.9
     Model the conductance-based neuron model.

  Parameters
  ----------
  size : int, sequence of int
    The network size of this neuron group.
  method: str
    The numerical integration method.
  name : optional, str
    The neuron group name.

  See Also
  --------
  Channel

  """

  def __init__(
      self,
      size: Shape,
      keep_size: bool = False,
      C: Union[float, Tensor, Initializer, Callable] = 1.,
      A: Union[float, Tensor, Initializer, Callable] = 1e-3,
      V_th: Union[float, Tensor, Initializer, Callable] = 0.,
      V_initializer: Union[Initializer, Callable, Tensor] = Uniform(-70, -60.),
      noise: Union[float, Tensor, Initializer, Callable] = None,
      method: str = 'exp_auto',
      name: str = None,
      mode: Mode = normal,
      **channels
  ):
    NeuGroup.__init__(self, size, keep_size=keep_size, mode=mode)
    Container.__init__(self, **channels, name=name, mode=mode)

    # parameters for neurons
    self.C = C
    self.A = A
    self.V_th = V_th
    self._V_initializer = V_initializer
    self.noise = init_noise(noise, self.varshape, num_vars=3)

    # variables
    self.V = variable(V_initializer, mode, self.varshape)
    self.input = variable(bm.zeros, mode, self.varshape)
    sp_type = bm.dftype() if isinstance(self.mode, BatchingMode) else bool
    self.spike = variable(lambda s: bm.zeros(s, dtype=sp_type), mode, self.varshape)

    # function
    if self.noise is None:
      self.integral = odeint(f=self.derivative, method=method)
    else:
      self.integral = sdeint(f=self.derivative, g=self.noise, method=method)

  def derivative(self, V, t):
    Iext = self.input.value * (1e-3 / self.A)
    channels = self.nodes(level=1, include_self=False).subset(Channel).unique()
    for ch in channels.values():
      Iext = Iext + ch.current(V)
    return Iext / self.C

  def reset_state(self, batch_size=None):
    self.V.value = variable(self._V_initializer, batch_size, self.varshape)
    sp_type = bm.dftype() if isinstance(self.mode, BatchingMode) else bool
    self.spike.value = variable(lambda s: bm.zeros(s, dtype=sp_type), batch_size, self.varshape)
    self.input.value = variable(bm.zeros, batch_size, self.varshape)

  def update(self, tdi, *args, **kwargs):
    V = self.integral(self.V.value, tdi['t'], tdi['dt'])
    channels = self.nodes(level=1, include_self=False).subset(Channel).unique()
    for node in channels.values():
      node.update(tdi, self.V.value)
    self.spike.value = bm.logical_and(V >= self.V_th, self.V < self.V_th)
    self.input[:] = 0.
    self.V.value = V

  def register_implicit_nodes(self, *channels, **named_channels):
    check_master(type(self), *channels, **named_channels)
    super(CondNeuGroup, self).register_implicit_nodes(*channels, **named_channels)


class Channel(DynamicalSystem):
  """Abstract channel class."""

  master_type = CondNeuGroup

  def __init__(
      self,
      size: Union[int, Sequence[int]],
      name: str = None,
      keep_size: bool = False,
      mode: Mode = normal,
  ):
    super(Channel, self).__init__(name=name, mode=mode)
    # the geometry size
    self.size = to_size(size)
    # the number of elements
    self.num = size2num(self.size)
    # variable shape
    self.keep_size = keep_size

  @property
  def varshape(self):
    return self.size if self.keep_size else self.num

  def update(self, tdi, V):
    raise NotImplementedError('Must be implemented by the subclass.')

  def current(self, V):
    raise NotImplementedError('Must be implemented by the subclass.')

  def reset_state(self, batch_size=None):
    raise NotImplementedError('Must be implemented by the subclass.')


def _check(master, child):
  if not hasattr(child, 'master_type'):
    raise ValueError('Child class should define "master_type" to specify the type of the master. '
                     f'But we did not found it in {child}')
  if not issubclass(master, child.master_type):
    raise TypeError(f'Type does not match. {child} requires a master with type '
                    f'of {child.master_type}, but the master now is {master}.')


def check_master(master, *channels, **named_channels):
  for channel in channels:
    if isinstance(channel, Channel):
      _check(master, channel)
    elif isinstance(channel, (list, tuple)):
      for ch in channel:
        _check(master, ch)
    elif isinstance(channel, dict):
      for ch in channel.values():
        _check(master, ch)
    else:
      raise ValueError(f'Do not support {type(channel)}.')
  for channel in named_channels.values():
    if not isinstance(channel, Channel):
      raise ValueError(f'Do not support {type(channel)}. ')
    _check(master, channel)


class Sequential(Container):
  def __init__(
      self,
      *modules,
      name: str = None,
      mode: Mode = normal,
      **kw_modules
  ):
    super(Sequential, self).__init__(*modules, name=name, mode=mode, **kw_modules)

  def __getattr__(self, item):
    """Wrap the dot access ('self.'). """
    child_ds = super(Sequential, self).__getattribute__('implicit_nodes')
    if item in child_ds:
      return child_ds[item]
    else:
      return super(Sequential, self).__getattribute__(item)

  def __getitem__(self, key: Union[int, slice]):
    if isinstance(key, str):
      if key not in self.implicit_nodes:
        raise KeyError(f'Does not find a component named {key} in\n {str(self)}')
      return self.implicit_nodes[key]
    elif isinstance(key, slice):
      keys = tuple(self.implicit_nodes.keys())[key]
      components = tuple(self.implicit_nodes.values())[key]
      return Sequential(dict(zip(keys, components)))
    elif isinstance(key, int):
      return self.implicit_nodes.values()[key]
    elif isinstance(key, (tuple, list)):
      all_keys = tuple(self.implicit_nodes.keys())
      all_vals = tuple(self.implicit_nodes.values())
      keys, vals = [], []
      for i in key:
        if isinstance(i, int):
          raise KeyError(f'We excepted a tuple/list of int, but we got {type(i)}')
        keys.append(all_keys[i])
        vals.append(all_vals[i])
      return Sequential(dict(zip(keys, vals)))
    else:
      raise KeyError(f'Unknown type of key: {type(key)}')

  def __repr__(self):
    def f(x):
      if not isinstance(x, DynamicalSystem) and callable(x):
        signature = inspect.signature(x)
        args = [f'{k}={v.default}' for k, v in signature.parameters.items()
                if v.default is not inspect.Parameter.empty]
        args = ', '.join(args)
        while not hasattr(x, '__name__'):
          if not hasattr(x, 'func'):
            break
          x = x.func  # Handle functools.partial
        if not hasattr(x, '__name__') and hasattr(x, '__class__'):
          return x.__class__.__name__
        if args:
          return f'{x.__name__}(*, {args})'
        return x.__name__
      else:
        x = repr(x).split('\n')
        x = [x[0]] + ['  ' + y for y in x[1:]]
        return '\n'.join(x)

    entries = '\n'.join(f'  [{i}] {f(x)}' for i, x in enumerate(self))
    return f'{self.__class__.__name__}(\n{entries}\n)'

  def update(self, sha: dict, x: Any) -> Tensor:
    """Update function of a sequential model.

    Parameters
    ----------
    sha: dict
      The shared arguments (ShA) across multiple layers.
    x: Any
      The input information.

    Returns
    -------
    y: Tensor
      The output tensor.
    """
    for node in self.implicit_nodes.values():
      x = node(sha, x)
    return x
