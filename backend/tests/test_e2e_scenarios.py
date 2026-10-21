import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.ocr.mock import load_mock_manifest
from app.parsing.questions import parse_ocr_page


def _generate(name: str, output_dir: Path):
    module = importlib.import_module("app.testing.e2e_scenarios")
    return module.generate_e2e_scenario(name, output_dir)


@pytest.mark.parametrize("name", ["A", "B", "C", "D", "E"])
def test_each_e2e_fixture_has_generated_images_and_complete_manifest(
    name: str,
    tmp_path: Path,
) -> None:
    fixture = _generate(name, tmp_path / name)
    results = load_mock_manifest(fixture.manifest_path)
    digests = {
        hashlib.sha256(image_path.read_bytes()).hexdigest()
        for image_path in fixture.image_paths
    }

    assert fixture.scenario_name == name
    assert fixture.import_directories
    assert fixture.image_paths
    assert digests == set(results)
    assert all(image_path.suffix == ".png" for image_path in fixture.image_paths)
    assert all((tmp_path / name) in image_path.parents for image_path in fixture.image_paths)


def test_scenario_a_contains_two_single_choice_and_one_true_false(
    tmp_path: Path,
) -> None:
    fixture = _generate("A", tmp_path / "A")
    parsed = [
        question
        for result in load_mock_manifest(fixture.manifest_path).values()
        for question in parse_ocr_page(result)
    ]

    assert [question.question_type for question in parsed].count("single_choice") == 2
    assert [question.question_type for question in parsed].count("true_false") == 1
    assert all(question.correct_answer is not None for question in parsed)
    assert all(not question.anomaly_flags for question in parsed)


def test_scenario_b_contains_a_missing_answer_anomaly(tmp_path: Path) -> None:
    fixture = _generate("B", tmp_path / "B")
    parsed = [
        question
        for result in load_mock_manifest(fixture.manifest_path).values()
        for question in parse_ocr_page(result)
    ]

    assert len(parsed) == 1
    assert "MISSING_ANSWER" in {flag.code for flag in parsed[0].anomaly_flags}


@pytest.mark.parametrize("name", ["C", "E"])
def test_cross_screenshot_scenarios_keep_directional_ocr_evidence(
    name: str,
    tmp_path: Path,
) -> None:
    fixture = _generate(name, tmp_path / name)
    raw_results = [
        result.raw_output
        for result in load_mock_manifest(fixture.manifest_path).values()
    ]
    relationships = {
        result.get("relationship")
        for result in raw_results
        if isinstance(result, dict)
    }

    assert {"cross-first", "cross-second"}.issubset(relationships)


def test_scenario_d_uses_two_folders_with_same_source_number_and_different_stems(
    tmp_path: Path,
) -> None:
    fixture = _generate("D", tmp_path / "D")
    parsed = [
        question
        for result in load_mock_manifest(fixture.manifest_path).values()
        for question in parse_ocr_page(result)
    ]

    assert len(fixture.import_directories) == 2
    assert len(parsed) == 2
    assert {question.source_display_number for question in parsed} == {"1"}
    assert len({question.stem for question in parsed}) == 2


def test_scenario_generator_cli_runs_in_an_isolated_process(tmp_path: Path) -> None:
    output_dir = tmp_path / "cli-A"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.testing.e2e_scenarios",
            "--scenario",
            "A",
            "--output",
            str(output_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["scenario"] == "A"
    assert (output_dir / "fixture.json").is_file()
