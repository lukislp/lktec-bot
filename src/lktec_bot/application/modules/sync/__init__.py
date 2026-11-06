"""The ``sync`` module: commit generated files onto a branch through the App.

``options`` is pure and imported by the module registry; ``service`` drives the gate and is
imported by the command line only, which keeps the import graph acyclic.
"""
