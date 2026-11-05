# Synthetic end-to-end example

This example removes duplicates from integer lists while preserving first occurrence. It is unrelated to any actual assessment. Seven hand-authored cases provide exact expected outputs. Both variants use the same check and fixture.

After installing the package, run with the package's Python interpreter:

```bash
.venv/bin/python examples/synthetic_demo/demo.py
```

On Windows, use `.venv\Scripts\python.exe` instead. An optional `--workspace PATH` must point to a new or empty directory. Existing workspace files are never overwritten. The default creates a new temporary directory and preserves its evidence for inspection.

The demo prepares inputs, runs both variants, preserves evidence, compares conditions, records an AI assessment with an explicit reporter, and exports a local review packet. It makes no external model calls and uploads nothing.

`wall_time_ms` is measured by the runner for the **whole subprocess**, including startup, imports, and correctness checks. Small cases can be dominated by startup. The example does not establish algorithm-only speed, memory use, asymptotic scaling, or general superiority. No timing values are hard-coded.
