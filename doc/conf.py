"""Sphinx configuration file for an LSST stack package.

This configuration only affects single-package Sphinx documentation builds.
"""

from documenteer.sphinxconfig.stackconf import build_package_configs
import lsst.dbb.buffmngrs.handoff


_g = globals()
_g.update(build_package_configs(
    project_name='dbb_buffmngrs_handoff',
    version=lsst.dbb.buffmngrs.handoff.version.__version__))
