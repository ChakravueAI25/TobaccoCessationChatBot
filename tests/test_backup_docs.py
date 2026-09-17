from pathlib import Path


def test_backup_command_documented():
    script = Path("scripts/backup.ps1")
    assert script.exists()
    assert "pg_dump" in script.read_text()