## Summary

Describe the problem and the outcome of this change.

## Validation

List the exact commands you ran and their results.

- [ ] `python -m pytest -q`
- [ ] Ruff, Mypy, and compile checks where applicable
- [ ] Web tests/build for visible workbench changes
- [ ] Memvid/MCP integration fixtures for those surfaces

## Safety and Compatibility

- [ ] I preserved the Memvid v2 `.mv2` storage invariants.
- [ ] I did not weaken capability enablement, exact-call approval, secret handling, or policy-memory gates.
- [ ] I included migration and rollback notes for persistent-state or configuration changes.
- [ ] I did not include credentials, `.nest/` state, private memory, caches, or unrelated generated files.
- [ ] I preserved the `nested-memvid-agent` distribution and `nested_memvid_agent` import identity, or clearly documented an approved compatibility change.

## Documentation and Release Notes

- [ ] Tests cover the changed behavior.
- [ ] User/operator documentation is updated where needed.
- [ ] `CHANGELOG.md` is updated for a user-visible change, or this change needs no changelog entry.
- [ ] Screenshots are attached for visible UI changes, or this change has no visible UI impact.

## Related Work

Link issues, design discussions, or prior pull requests.
