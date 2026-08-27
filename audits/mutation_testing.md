# Mutation-testing result

Status: **NOT COMPLETED — tool/platform incompatibility**

Two bounded attempts were made against the critical execution-protocol module:

```bash
uvx mutmut --help
```

Result: `NotImplementedError: only implemented on linux and mac` on native Windows.

```bash
uvx --with setuptools mutatest -s src/execution_protocol.py -n 10 -r 20260826 --nocov -t '.venv/Scripts/python.exe -m pytest tests/test_execution_protocol.py -q' -o audits/mutation_execution_protocol.rst
```

Result: `AttributeError: 'Constant' object has no attribute 'kind'`, reflecting `mutatest` incompatibility with the Python 3.11 AST.

No mutation score is claimed. Critical modules were still exercised by unit, integration, property-based, and real-subprocess tests, but this is not equivalent to mutation testing. A Linux CI job with a maintained Python-3.11-compatible mutation tool remains an uncovered verification item.