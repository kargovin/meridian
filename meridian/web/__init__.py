"""The application's HTTP tier.

Holds the admin surface over the source registry (FR-I2/FR-I6) and, later, the reader
surface. Both run in one process: A2 makes this a modular monolith, so the seam between
them is a module boundary rather than a deployment one.
"""
