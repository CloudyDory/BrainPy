# -*- coding: utf-8 -*-

__version__ = "2.0.3"


try:
  import jaxlib
  del jaxlib
except ModuleNotFoundError:
  raise ModuleNotFoundError(
    'Please install jaxlib. See '
    'https://brainpy.readthedocs.io/en/latest/quickstart/installation.html#dependency-2-jax '
    'for installation instructions.'
  )


# fundamental modules
from . import errors, tools


# "base" module
from . import base
from .base.base import Base
from .base.collector import Collector, TensorCollector


# "math" module
from . import math


# tool modules
from . import connect , initialize, optimizers, datasets, measure, losses


# "integrators" module
from . import integrators
from .integrators import ode
from .integrators import sde
from .integrators.ode import odeint
from .integrators.ode import set_default_odeint
from .integrators.ode import get_default_odeint
from .integrators.sde import sdeint
from .integrators.sde import set_default_sdeint
from .integrators.sde import get_default_sdeint
from .integrators.joint_eq import JointEq


# "simulation" module
from . import dynsim
from .dynsim import inputs


# "rnn" module
from . import rnn


# "running" module
from . import running


# "analysis" module
from . import analysis


# "visualization" module
from .visualization import visualize

# compatible interface
from .compact import *  # compact

conn = connect
init = initialize
opt = optimizers
