# Judge-human label sessions

Files here are written by `attune eval label` (see
`src/attune/evals/agreement.py::run_label_session`) — one JSONL file per
labeling session (`<domain>.jsonl` by convention), each line a
`{case_id, domain, human_choice, judge_choice}` record.

`compute_agreement` over these records is what populates `evals/agreement.json`
— the per-domain judge-human agreement rate that gates whether that domain's
pairwise result can fail CI (below 75%, it can't; see
`src/attune/evals/agreement.py`'s module docstring and `docs/decisions.md`).
