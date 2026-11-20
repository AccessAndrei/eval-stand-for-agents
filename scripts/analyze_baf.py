"""Анализ содержания оценочных задач и сохранённых результатов."""

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from statistics import mean, median
from typing import Any

import yaml

from baf.runner.batch import CONTINUE_EXECUTION
from baf.runner.catalog import Task, load_dataset
from baf.runner.evidence import mounted_b5_evidence, validate_evidence
from contracts.mounted_report import validate_mounted_report
from contracts.validation import ContractError, validate

ROOT = Path(__file__).resolve().parents[1]
Row = dict[str, Any]


def source_name(path: Path) -> str:
    """Использовать переносимый путь для файлов внутри репозитория."""
    resolved = path.resolve()
    return (
        resolved.relative_to(ROOT).as_posix()
        if resolved.is_relative_to(ROOT)
        else resolved.as_posix()
    )


def specifications(path: Path) -> list[Row]:
    catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = [
        {
            "task_id": item["id"],
            "baf": baf,
            "function": group["name"],
            "level": item["level"],
            "prompt": item["prompt"],
            "check": item["check"],
            "decomposition": "",
        }
        for baf, group in catalog["bafs"].items()
        for item in group["tasks"]
    ]
    rows += [
        {
            "task_id": item["id"],
            "baf": "composite",
            "function": "Составная задача",
            "level": "composite",
            "prompt": item["prompt"],
            "check": item["check"],
            "decomposition": " → ".join(item["decomposition"]),
        }
        for item in catalog["composite"]
    ]
    ids = [row["task_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Каталог содержит повторяющиеся идентификаторы задач")
    return rows


def task_data(task: Task) -> Row:
    data = yaml.safe_load((task.directory / "task.yaml").read_text(encoding="utf-8"))
    return {
        "ready": True,
        "directory": source_name(task.directory),
        "baf": task.baf,
        "level": data.get("level", ""),
        "execution_prompt": "\n".join(task.messages),
        "timeout_s": task.timeout_s,
        "evidence_required": task.evidence_required,
    }


def inventory_entry(task: Task, spec: Row | None = None) -> Row:
    actual = task_data(task)
    if spec is not None and all(actual[key] == spec[key] for key in ("baf", "level")):
        return {**spec, **actual}
    return {
        "task_id": task.id,
        "function": task.baf,
        "prompt": actual["execution_prompt"],
        "check": "",
        "decomposition": "",
        **actual,
    }


def inventory(specs: list[Row], tasks: tuple[Task, ...]) -> list[Row]:
    by_id = {task.id: task for task in tasks}
    rows = []
    for spec in specs:
        task = by_id.pop(spec["task_id"], None)
        row = inventory_entry(task, spec) if task else {**spec, "ready": False}
        rows.append(row)
    rows.extend(inventory_entry(task) for task in by_id.values())
    return rows


def report_version(report: Any) -> str:
    version = report.get("version") if isinstance(report, dict) else None
    if version not in ("v0", "v1"):
        raise ContractError("Неподдерживаемая версия отчёта")
    return version


def _check_mounted_verdict(task: Task, entry: Row, result: Row) -> None:
    if entry["checker_status"] not in {"passed", "failed", "checker_error"}:
        return
    if result["execution_status"] not in CONTINUE_EXECUTION:
        raise ContractError("Проверка указана при инфраструктурном исходе")
    if task.evidence_required:
        if entry["evidence_status"] != "complete":
            raise ContractError("Проверка задачи требует полного доказательства")
        proof = validate_evidence(
            mounted_b5_evidence(result, task.input_manifest),
            run_id=entry["run_id"],
            input_manifest=task.input_manifest,
        )
        if not proof.complete or (
            entry["checker_status"] == "passed" and not proof.passed
        ):
            raise ContractError("Вердикт задачи расходится с доказательством")


def runs_from_report(report: Row, tasks: tuple[Task, ...]) -> list[Row]:
    version = report_version(report)
    if version == "v1":
        validate_mounted_report(report)
    else:
        validate("report", report)
    if [task.id for task in tasks] != [row["task_id"] for row in report["tasks"]]:
        raise ValueError("Состав или порядок задач в отчёте расходится с датасетом")
    rows = []
    for task, entry in zip(tasks, report["tasks"], strict=True):
        result = entry["result"] or {}
        tokens = result.get("tokens", {})
        snapshot = result.get("provider_snapshot") or {}
        provenance = result.get("provenance", {})
        row = {
            "task_id": entry["task_id"],
            "run_id": entry["run_id"],
            "checker_status": entry["checker_status"],
            "execution_status": result.get("execution_status"),
            "collection_status": result.get("collection_status"),
            "cleanup_status": result.get("cleanup_status"),
            "evidence_status": result.get("evidence_status"),
            "duration_s": result.get("duration_s"),
            "tokens_input": tokens.get("input"),
            "tokens_output": tokens.get("output"),
            "tokens_cached": tokens.get("cached"),
            "tokens_total": tokens.get("actual"),
            "model": result.get("model"),
            "provider_profile": result.get("provider_profile"),
            "budget_mode": tokens.get("mode"),
            "protocol": snapshot.get("protocol"),
            "codex_version": provenance.get("codex_version"),
            "source_commit": provenance.get("commit"),
            "image_digest": provenance.get("image_digest"),
        }
        if version == "v1":
            _check_mounted_verdict(task, entry, result)
            row.update(
                session_id=entry["session_id"],
                trace_id=result.get("trace_id"),
                files_status=entry["files_status"],
                cleanup_status=entry["cleanup_status"],
                evidence_status=entry["evidence_status"],
                checker_reason=entry["checker_reason"],
                skip_reason=entry["skip_reason"],
                attempt_started_at=entry["started_at"],
                attempt_finished_at=entry["finished_at"],
                attempt_duration_s=entry["duration_s"],
                tokens_unknown_reason=tokens.get("unknown_reason"),
                provenance_origin=provenance.get("origin"),
                architecture=provenance.get("architecture"),
                trace_path=entry["trace_path"],
                artifact_path=entry["artifact_path"],
            )
        rows.append(row)
    return rows


def describe(values: list[int | float]) -> Row:
    return {
        "count": len(values),
        "sum": sum(values),
        "min": min(values) if values else None,
        "median": median(values) if values else None,
        "mean": mean(values) if values else None,
        "max": max(values) if values else None,
    }


def summarize(task_rows: list[Row], runs: list[Row]) -> Row:
    ready = [row for row in task_rows if row["ready"]]
    known_tokens = [
        row["tokens_total"] for row in runs if row["tokens_total"] is not None
    ]
    return {
        "specification": {
            "tasks": len(task_rows),
            "by_function": dict(Counter(row["baf"] for row in task_rows)),
            "by_level": dict(Counter(row["level"] for row in task_rows)),
        },
        "ready": {
            "tasks": len(ready),
            "by_function": dict(Counter(row["baf"] for row in ready)),
            "by_level": dict(Counter(row["level"] for row in ready)),
            "description_only_ids": [
                row["task_id"] for row in task_rows if not row["ready"]
            ],
        },
        "results": {
            "runs": len(runs),
            "checker_statuses": dict(Counter(row["checker_status"] for row in runs)),
            "models": sorted({row["model"] for row in runs if row["model"]}),
            "duration_s": describe(
                [row["duration_s"] for row in runs if row["duration_s"] is not None]
            ),
            "tokens_total": describe(known_tokens),
            "tokens_input": sum(row["tokens_input"] or 0 for row in runs),
            "tokens_output": sum(row["tokens_output"] or 0 for row in runs),
            "tokens_cached": sum(row["tokens_cached"] or 0 for row in runs),
            "unknown_token_runs": len(runs) - len(known_tokens),
            "missing_measurements": {
                key: sum(row[key] is None for row in runs)
                for key in (
                    "duration_s",
                    "tokens_input",
                    "tokens_output",
                    "tokens_cached",
                )
            },
        },
    }


def write_csv(path: Path, rows: list[Row]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def plot(runs: list[Row], output: Path) -> None:
    """Построить график времени и токенов при установленном Matplotlib."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {"font.size": 10, "axes.spines.top": False, "axes.spines.right": False}
    )
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.5), layout="constrained")
    ids = [row["task_id"] for row in runs]
    durations = [
        row["duration_s"] if row["duration_s"] is not None else float("nan")
        for row in runs
    ]
    axes[0].bar(ids, durations, color="#246fa8")
    axes[0].set(ylabel="Секунды", title="Время выполнения")
    inputs = [
        row["tokens_input"] if row["tokens_input"] is not None else float("nan")
        for row in runs
    ]
    outputs = [
        row["tokens_output"] if row["tokens_output"] is not None else float("nan")
        for row in runs
    ]
    axes[1].bar(ids, inputs, label="Входные", color="#246fa8")
    axes[1].bar(
        ids,
        outputs,
        bottom=inputs,
        label="Выходные",
        color="#d58d37",
    )
    axes[1].set(ylabel="Токены", title="Расход токенов")
    axes[1].legend()
    for index, row in enumerate(runs):
        if row["duration_s"] is None:
            axes[0].text(index, 0, "нет данных", rotation=90, ha="center", va="bottom")
        if row["tokens_input"] is None or row["tokens_output"] is None:
            axes[1].text(index, 0, "нет данных", rotation=90, ha="center", va="bottom")
    fig.savefig(
        output / "run_metrics.png",
        dpi=160,
        metadata={"Software": "eval-stand-for-agents"},
    )
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "baf/catalog.yaml")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/eda")
    parser.add_argument(
        "--plots", action="store_true", help="Построить графики (Matplotlib)"
    )
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    version = report_version(report)
    tasks = load_dataset(args.dataset, mounted=version == "v1")
    runs = runs_from_report(report, tasks)
    task_rows = inventory(specifications(args.catalog), tasks)
    summary = summarize(task_rows, runs)
    wall_s = (
        datetime.fromisoformat(report["finished_at"])
        - datetime.fromisoformat(report["started_at"])
    ).total_seconds()
    summary["results"].update(
        {
            "batch_id": report["batch_id"],
            "started_at": report["started_at"],
            "finished_at": report["finished_at"],
            "batch_wall_s": wall_s,
            "model_metadata_missing": [
                key
                for key in ("model_version", "intelligence_index", "checked_at")
                if report.get("course_model_status", {}).get(key) is None
            ],
        }
    )
    if version == "v1":
        summary["results"].update(
            {
                "report_version": version,
                **{
                    field: report[field]
                    for field in (
                        "evaluated",
                        "wrong_answers",
                        "technical_skips",
                        "success",
                    )
                },
            }
        )
    sources = {
        args.catalog.resolve(),
        args.dataset.resolve(),
        args.report.resolve(),
        Path(__file__).resolve(),
        ROOT / "baf/runner/catalog.py",
        ROOT / "baf/runner/task_checks.py",
        ROOT / "baf/runner/evidence.py",
        ROOT / "contracts/validation.py",
        ROOT / "contracts/v0.schema.json",
    }
    if version == "v1":
        sources.update(
            {
                ROOT / "baf/runner/mounted_files.py",
                ROOT / "contracts/mounted.py",
                ROOT / "contracts/mounted.schema.json",
                ROOT / "contracts/mounted_report.py",
                ROOT / "contracts/mounted_report.schema.json",
            }
        )
    for task in tasks:
        sources.update({task.directory / "task.yaml", task.checker_path})
        sources.update(task.directory.glob("*.json"))
        sources.update(
            task.directory / "fixture" / file.path for file in task.fixture_files
        )
    summary["sources"] = [
        {"path": source_name(path), "sha256": sha256(path.read_bytes()).hexdigest()}
        for path in sorted(sources)
    ]
    args.output.mkdir(parents=True, exist_ok=True)
    for name, rows in (
        ("task_inventory", task_rows),
        ("runs", runs),
    ):
        write_csv(args.output / f"{name}.csv", rows)
    (args.output / "summary.json").write_text(
        json.dumps(
            summary, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n",
        encoding="utf-8",
    )
    if args.plots:
        plot(runs, args.output / "figures")
    print(f"Данные EDA сохранены в {args.output}")


if __name__ == "__main__":
    main()
