"""Domain core: models, ports and (from P1) pure domain logic and use cases.

Must never import ``adapters``, ``app``, ``api``, ``worker`` or IO/framework libraries
(SPEC D-02). Enforced by ``tests/unit/test_architecture.py``.
"""
