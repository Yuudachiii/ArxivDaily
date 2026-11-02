import json
from pathlib import Path
import tomllib

import yaml
import pytest

from arxivdaily.cli import main


def test_workflow_config_generation_preserves_committed_custom_config(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.load((root / ".github/workflows/daily-digest.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["digest"]["steps"]
    prepare = next(step["run"] for step in steps if step["name"] == "Prepare public configuration")
    script = prepare.split("python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.chdir(app)
    monkeypatch.setenv("REPOSITORY", "reader/digest")
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    config = tmp_path / "config.toml"
    config.write_text('[archive]\npublic_base_url = ""\nstate_branch = "reader-state"\n', encoding="utf-8")
    exec(script, {})
    assert tomllib.loads(config.read_text(encoding="utf-8"))["archive"]["public_base_url"] == "https://raw.githubusercontent.com/reader/digest/reader-state"
    assert output.read_text(encoding="utf-8").strip() == "state_branch=reader-state"
    custom = '[archive]\npublic_base_url = "https://example.org/custom-state"\n'
    config.write_text(custom, encoding="utf-8")
    exec(script, {})
    assert config.read_text(encoding="utf-8") == custom


@pytest.mark.parametrize("repository", ["owner/upstream", "classmate/daily"])
def test_committed_config_generates_archive_url_for_current_repository(tmp_path, monkeypatch, repository):
    """Exercise the published default config, not just a synthetic empty URL."""
    import shutil
    from arxivdaily.config import load_config
    root = Path(__file__).resolve().parents[2]
    source = (root / "config.toml").read_text(encoding="utf-8")
    assert tomllib.loads(source)["archive"]["public_base_url"] == ""
    config = tmp_path / "config.toml"
    config.write_text(source, encoding="utf-8")
    app = tmp_path / "app"
    app.mkdir()
    shutil.copytree(root / "app/prompts", app / "prompts")
    workflow = yaml.load((root / ".github/workflows/daily-digest.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    prepare = next(step["run"] for step in workflow["jobs"]["digest"]["steps"] if step["name"] == "Prepare public configuration")
    script = prepare.split("python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    monkeypatch.chdir(app)
    monkeypatch.setenv("REPOSITORY", repository)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github-output"))
    exec(script, {})
    cfg = load_config(config)
    assert cfg["archive"]["public_base_url"] == f"https://raw.githubusercontent.com/{repository}/{cfg['archive']['state_branch']}"
    assert cfg["presentation"] == tomllib.loads(source)["presentation"]
    assert tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))["archive"]["public_base_url"] == ""


def test_default_dry_run_cannot_write_normal_state(tmp_path):
    report = tmp_path / "report.json"
    normal = tmp_path / "normal"
    code = main(["run", "--state-dir", str(normal), "--now", "2026-10-02T07:17:00+00:00", "--report", str(report)])
    data = json.loads(report.read_text(encoding="utf-8"))
    assert code == 0 and data["dry_run_isolated"] and not normal.exists()


def test_live_send_requires_public_archive_and_publisher_before_any_io(tmp_path):
    report = tmp_path / "report.json"
    assert main(["run", "--send", "--report", str(report)]) == 2
    data = json.loads(report.read_text(encoding="utf-8"))
    assert "checkpoint" in data["error"]


def test_simulation_rejects_normal_state_directory(tmp_path):
    report = tmp_path / "report.json"
    assert main(["simulate", "--state-dir", str(tmp_path/"normal"), "--report", str(report)]) == 2
    assert "simulation state" in json.loads(report.read_text(encoding="utf-8"))["error"]


def test_missing_send_credentials_fail_before_external_work(tmp_path,monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY",raising=False)
    monkeypatch.delenv("SERVERCHAN_SENDKEY",raising=False)
    config = tmp_path / "config.toml"
    config.write_text('[archive]\npublic_base_url = "https://example.org/state"\n',encoding="utf-8")
    report = tmp_path / "report.json"
    code = main(["run","--send","--config",str(config),"--checkpoint-command","never-execute","--report",str(report)])
    assert code == 2
    error = json.loads(report.read_text(encoding="utf-8"))["error"]
    assert "DEEPSEEK_API_KEY" in error and "SERVERCHAN_SENDKEY" in error


def test_rules_mode_send_requires_only_the_push_key(tmp_path, monkeypatch):
    from arxivdaily.cli import required_credentials
    from arxivdaily.config import load_config
    config = load_config()
    config["selection"]["mode"] = "rules"
    config["translation"]["provider"] = "google_free"
    assert required_credentials(config) == [config["delivery"]["sendkey_env"]]
    config["translation"]["provider"] = "microsoft_free"
    assert required_credentials(config) == [config["delivery"]["sendkey_env"]]
    config["translation"]["provider"] = "deepl"
    assert required_credentials(config) == sorted({config["delivery"]["sendkey_env"], config["translation"]["api_key_env"]})

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TRANSLATION_API_KEY", raising=False)
    monkeypatch.delenv("SERVERCHAN_SENDKEY", raising=False)
    path = tmp_path / "config.toml"
    path.write_text('[selection]\nmode = "rules"\n[archive]\npublic_base_url = "https://example.org/state"\n', encoding="utf-8")
    report = tmp_path / "report.json"
    assert main(["run", "--send", "--config", str(path), "--checkpoint-command", "never-execute", "--report", str(report)]) == 2
    error = json.loads(report.read_text(encoding="utf-8"))["error"]
    assert "SERVERCHAN_SENDKEY" in error and "DEEPSEEK_API_KEY" not in error


def test_workflow_matrix_has_two_translation_variants_and_no_llm_secret():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.load((root / ".github/workflows/daily-digest.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["digest"]
    variants = job["strategy"]["matrix"]["translation"]
    assert {item["provider"] for item in variants} == {"google_free", "microsoft_free"}
    branches = {item["state_branch"] for item in variants}
    assert branches == {"arxivdaily-state-google", "arxivdaily-state-microsoft"}
    assert job["concurrency"]["group"] == "arxivdaily-state-${{ matrix.translation.state }}"
    assert "concurrency" not in workflow  # per-variant groups, not a shared workflow lock
    run_step = next(step for step in job["steps"] if step["name"] == "Run digest and checkpoint before side effects")
    assert "DEEPSEEK_API_KEY" not in run_step.get("env", {})
    prepare = next(step for step in job["steps"] if step["name"] == "Prepare public configuration")
    assert prepare["env"]["TRANSLATION_PROVIDER"] == "${{ matrix.translation.provider }}"
    assert prepare["env"]["STATE_BRANCH"] == "${{ matrix.translation.state_branch }}"


def test_workflow_prepare_rewrites_translation_variant_and_state_branch(tmp_path, monkeypatch):
    import shutil
    from arxivdaily.config import load_config
    root = Path(__file__).resolve().parents[2]
    app = tmp_path / "app"
    app.mkdir()
    shutil.copytree(root / "app/prompts", app / "prompts")
    config = tmp_path / "config.toml"
    config.write_text((root / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
    workflow = yaml.load((root / ".github/workflows/daily-digest.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    prepare = next(step["run"] for step in workflow["jobs"]["digest"]["steps"] if step["name"] == "Prepare public configuration")
    script = prepare.split("python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    monkeypatch.chdir(app)
    monkeypatch.setenv("REPOSITORY", "reader/llm-digest")
    monkeypatch.setenv("TRANSLATION_PROVIDER", "microsoft_free")
    monkeypatch.setenv("TRANSLATION_LABEL", "微软")
    monkeypatch.setenv("STATE_BRANCH", "arxivdaily-state-microsoft")
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    exec(script, {})
    updated = load_config(config)
    assert updated["translation"]["provider"] == "microsoft_free"
    assert updated["translation"]["label"] == "微软"
    assert updated["archive"]["state_branch"] == "arxivdaily-state-microsoft"
    assert updated["archive"]["public_base_url"] == "https://raw.githubusercontent.com/reader/llm-digest/arxivdaily-state-microsoft"
    assert tomllib.loads(config.read_text(encoding="utf-8"))["presentation"] == tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))["presentation"]
    assert output.read_text(encoding="utf-8").strip() == "state_branch=arxivdaily-state-microsoft"


def test_delivery_only_recovery_does_not_require_deepseek_key(tmp_path,monkeypatch):
    from arxivdaily import cli
    from arxivdaily.config import load_config
    from arxivdaily.delivery import MockDelivery
    from arxivdaily.models import parse_time
    from arxivdaily.pipeline import Pipeline
    from arxivdaily.simulation import FixtureAnalysis,FixtureReader,load_fixture
    from arxivdaily.state import StateStore
    app = Path(__file__).resolve().parents[1]
    fixture_path = app/"fixtures/demo.json"
    fixture,papers = load_fixture(fixture_path)
    analysis = FixtureAnalysis(fixture)
    reader = FixtureReader(load_config(),fixture,fixture_path,tmp_path/"raw")
    state_dir = tmp_path/"state"
    Pipeline(load_config(),StateStore(state_dir),None,analysis,reader,MockDelivery("unknown"),now=parse_time("2026-10-02T07:17:00+00:00")).run(papers[:1])
    analysis.calls = {"selection":0,"review":0,"summary":0}
    monkeypatch.delenv("DEEPSEEK_API_KEY",raising=False)
    monkeypatch.setenv("SERVERCHAN_SENDKEY","fake-runtime-sendkey")
    monkeypatch.setattr(cli,"AnalysisClient",lambda config:analysis)
    monkeypatch.setattr(cli,"ServerChan",lambda config:MockDelivery("confirmed"))
    monkeypatch.setattr(cli,"command_publisher",lambda command:lambda:None)
    config_path = tmp_path/"config.toml"
    config_path.write_text('[archive]\npublic_base_url = "https://example.org/state"\n',encoding="utf-8")
    code = main(["run","--send","--retry-stage","delivery","--retry-ids",papers[0].base_id,"--state-dir",str(state_dir),"--config",str(config_path),"--checkpoint-command","mock-only","--report",str(tmp_path/"recovery.json")])
    assert code == 0 and analysis.calls == {"selection":0,"review":0,"summary":0}
