# -*- coding: utf-8 -*-
"""
Created on Sat Mar 16 13:38:11 2024

@author: cortespea
"""
import biosteam as bst
from .profile import register

__all__ = (
    'create_system_hydrocarbon_narrow_flash',
    'create_system_hydrocarbon_wide_flash',
)

@register(
    'hydrocarbon_narrow_flash', 'Hydrocarbon flash narrow',
    0.02, [0.004, 0.008, 0.012, 0.016, 0.02]
)
def create_system_hydrocarbon_narrow_flash(alg):
    bst.settings.set_thermo(['heptane', 'octane'], cache=True, Gamma=bst.IdealActivityCoefficients)
    feed = bst.Stream('feed', heptane=100, octane=100)
    recycle = bst.Stream('liquid_recycle')
    liquid_product = bst.Stream('liquid_product')
    vapor_product = bst.Stream('vapor_product')
    stage = bst.StageEquilibrium(
        'stage', ins=[feed, recycle], outs=[vapor_product, recycle, liquid_product], 
        B=1, bottom_split=0.4, phases=('g', 'l')
    )
    sys = bst.System.from_units('sys', [stage])
    return sys

@register(
    'hydrocarbon_wide_flash', 'Hydrocarbon flash wide',
    0.02, [0.004, 0.008, 0.012, 0.016, 0.02]
)
def create_system_hydrocarbon_wide_flash(alg):
    bst.settings.set_thermo(['propane', 'octane'], cache=True, Gamma=bst.IdealActivityCoefficients)
    feed = bst.Stream('feed', propane=100, octane=100)
    recycle = bst.Stream('liquid_recycle')
    liquid_product = bst.Stream('liquid_product')
    vapor_product = bst.Stream('vapor_product')
    stage = bst.StageEquilibrium(
        'stage', ins=[feed, recycle], outs=[vapor_product, recycle, liquid_product], 
        B=1, bottom_split=0.4, phases=('g', 'l')
    )
    sys = bst.System.from_units('sys', [stage])
    return sys
