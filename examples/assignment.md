Implement CSV export for the existing report command.

Scope:
- Inspect the report entry point and existing output tests.
- Add --format csv while preserving the current default output.
- Keep changes within the report module, its tests, and the relevant help text.
- Do not modify unrelated files, manage services, or publish anything.

Acceptance checks:
- Add coverage for a non-empty report and an empty report.
- Run the focused report tests and report the actual command and result.
- Leave no generated cache/build files in the patch.

Return a concise summary of changed files, tests actually run, and any blocker.
