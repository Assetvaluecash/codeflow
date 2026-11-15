-- PRD FR-007 build artifacts: a build of a revision (status, pipeline, the artifacts it produced) is recorded evidence, so a deployment can
-- be traced to the build that produced its artifact and a report can say whether the proposed revision built.
alter table evidence_records drop constraint evidence_records_kind_check;
alter table evidence_records add constraint evidence_records_kind_check check (kind in (
    'test_run', 'coverage', 'runtime_edge', 'deployment', 'incident', 'verdict',
    'approval', 'acknowledgement', 'notification', 'itsm_attachment', 'release', 'incident_link', 'build'));
