"""Apply resolved configuration to modules with process-wide caches."""
from __future__ import annotations


from argus.config import ArgusConfig




def configure(settings: ArgusConfig, *, load_team_rules: bool = True) -> None:
    from argus.operations import rule_runtime

    if load_team_rules:
        rule_runtime.configure(settings)
    else:
        rule_runtime.reset()
    from argus.core import users
    from argus.domain import history_index, workflow_rules
    from argus.operations import audit_index, audit_lock, control, diff_and_notify
    from argus.pipeline import attachment_quarantine, model_client, env_check, extractor, provenance
    from argus.reporting import publisher, report, server

    users.configure(settings.general.state_dir)
    history_index.configure(
        settings.general.state_dir,
        refresh_interval_s=getattr(
            settings.batch, "history_refresh_seconds", None),
    )
    control.configure(settings.general.state_dir)
    audit_lock.reset()
    diff_and_notify.configure(settings.general.state_dir)
    workflow_rules.configure(settings.policy)
    provenance.configure(settings)
    env_check.configure(settings.policy)
    extractor.configure_policy(settings.policy)
    extractor.configure_credentials(settings)
    attachment_quarantine.configure(settings)
    model_client.configure(settings)
    publisher.configure(settings)
    audit_index.configure(settings)
    report.configure(settings.extractor.base_url)
    server.configure_sections(settings.folders.dashboard_sections)
