"""UC1-08/09/10: ORM + migration extraction (Java JPA / Spring Data / Flyway / Liquibase, Python SQLAlchemy / SQLModel / Django /
Alembic) and data-impact analysis (AC-024): a migration that drops a column is traced to the services whose ORM mappings read
or write that table."""
import copy

import pytest
from fastapi.testclient import TestClient

from conftest import fresh_store, make_app
from fixtures.build_data_fixture import build as build_data
from tia import EXTRACTOR_VERSION, gitutil
from tia.data import diff_data, order_migrations, schema_state, type_widened
from tia.extract_data import (extract_file as extract_data_file, is_liquibase_path, is_sql_migration, norm_ident, parse_sql, snake)
from tia.extract_python import extract_repo, jdbc_database_id
from tia.extraction import extract_commit
from tia.graph import build_graph
from tia.impact import AnalysisRequest, analyze
from tia.report import redact, render_comment


@pytest.fixture(scope="module")
def dfx(tmp_path_factory):
    return build_data(str(tmp_path_factory.mktemp("dlandscape")))


def run(dfx, target, head, store=None, base="main", **kw):
    return analyze(AnalysisRequest(repos=dfx["paths"], target=target, base=base, head=head, **kw), store)


def ex(files, repo="svc"):
    return extract_repo(repo, "c", files)


def jpa(src, name="src/main/java/com/acme/X.java", extra=None):
    return ex({name: src, **(extra or {})})


def ops(sql):
    o, bad = parse_sql(sql)
    return o, bad


def kinds(rep):
    return {(f["component"], f["node_type"], f["severity_rule"]) for f in rep["findings"]}


def comps(rep, node_type=None):
    return {f["component"] for f in rep["findings"] if node_type is None or f["node_type"] == node_type}


# =====================================================================================================================
# SQL migrations
# =====================================================================================================================
def test_create_table_columns_types_nullability_and_keys():
    o, bad = ops("""-- schema
    CREATE TABLE public."Orders" (
        id BIGSERIAL PRIMARY KEY,
        customer_id bigint NOT NULL REFERENCES customers(id),
        status VARCHAR(20) NOT NULL DEFAULT 'NEW', /* lifecycle; not a statement end */
        total NUMERIC(12, 2),
        CONSTRAINT chk CHECK (total >= 0),
        PRIMARY KEY (id)
    );""")
    assert not bad and len(o) == 1 and o[0]["op"] == "create_table" and o[0]["table"] == "orders"
    cols = o[0]["columns"]
    assert cols["id"] == {"type": "bigserial", "nullable": False, "pk": True}
    assert cols["customer_id"]["nullable"] is False and cols["total"] == {"type": "numeric(12,2)", "nullable": True, "pk": False}
    assert cols["status"]["type"] == "varchar(20)" and "chk" not in cols
    assert o[0]["line"] == 2


def test_alter_table_actions_across_dialects():
    o, bad = ops("""
    ALTER TABLE orders ADD COLUMN note text;
    ALTER TABLE orders ADD COLUMN region varchar(10) NOT NULL, DROP COLUMN legacy;
    ALTER TABLE orders ALTER COLUMN status TYPE varchar(40);
    ALTER TABLE orders ALTER COLUMN total SET NOT NULL;
    ALTER TABLE orders ALTER COLUMN total DROP NOT NULL;
    ALTER TABLE orders RENAME COLUMN total TO amount;
    ALTER TABLE orders RENAME TO purchase_orders;
    ALTER TABLE t MODIFY COLUMN c VARCHAR(30) NOT NULL;
    ALTER TABLE t CHANGE old_c new_c INT;
    ALTER TABLE t ADD (a NUMBER(5), b VARCHAR2(10) NOT NULL);
    ALTER TABLE t DROP COLUMN x, y;
    ALTER TABLE t ALTER COLUMN z INT NOT NULL;
    """)
    assert not bad
    seen = [(x["op"], x["table"], x.get("column"), x.get("new_column") or x.get("new_table"), x.get("type"), x.get("nullable")) for x in o]
    assert ("add_column", "orders", "note", None, "text", True) in seen
    assert ("add_column", "orders", "region", None, "varchar(10)", False) in seen and ("drop_column", "orders", "legacy", None, None, None) in seen
    assert ("alter_column", "orders", "status", None, "varchar(40)", None) in seen
    assert ("alter_column", "orders", "total", None, None, False) in seen and ("alter_column", "orders", "total", None, None, True) in seen
    assert ("rename_column", "orders", "total", "amount", None, None) in seen and ("rename_table", "orders", None, "purchase_orders", None, None) in seen
    assert ("alter_column", "t", "c", None, "varchar(30)", False) in seen                                  # MySQL MODIFY
    assert ("rename_column", "t", "old_c", "new_c", None, None) in seen                                    # MySQL CHANGE
    assert ("add_column", "t", "a", None, "number(5)", True) in seen and ("add_column", "t", "b", None, "varchar2(10)", False) in seen   # Oracle
    assert ("drop_column", "t", "x", None, None, None) in seen and ("drop_column", "t", "y", None, None, None) in seen  # T-SQL list
    assert ("alter_column", "t", "z", None, "int", False) in seen                                          # T-SQL ALTER COLUMN


def test_drop_and_rename_table_and_ignored_statements():
    o, bad = ops("""DROP TABLE IF EXISTS x, y CASCADE;
    RENAME TABLE a TO b, c TO d;
    CREATE INDEX i ON t(c); CREATE SEQUENCE s; COMMENT ON TABLE t IS 'x;y';
    INSERT INTO t VALUES (1, 'a;b'); UPDATE t SET c = 1; DELETE FROM t; GRANT SELECT ON t TO r;
    CREATE TEMPORARY TABLE scratch (id int);
    """)
    assert not bad
    assert [(x["op"], x["table"], x.get("new_table")) for x in o] == [
        ("drop_table", "x", None), ("drop_table", "y", None), ("rename_table", "a", "b"), ("rename_table", "c", "d")]


def test_procedural_sql_is_reported_and_following_statements_still_parse():
    o, bad = ops("""CREATE OR REPLACE FUNCTION f() RETURNS trigger AS $$
    BEGIN
      UPDATE orders SET status = 'x'; RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    ALTER TABLE z DROP COLUMN q;
    CREATE PROCEDURE p AS
    BEGIN
      UPDATE a SET b = CASE WHEN c THEN 1 ELSE 2 END;
      DELETE FROM a;
    END;
    ALTER TABLE w DROP COLUMN k;
    BEGIN;
    ALTER TABLE v DROP COLUMN j;
    COMMIT;
    CALL archive();
    FROBNICATE everything;
    """)
    assert [(x["table"], x["column"]) for x in o] == [("z", "q"), ("w", "k"), ("v", "j")]
    reasons = [b["reason"] for b in bad]
    assert sum("procedural SQL" in r for r in reasons) == 2 and any("stored-procedure call" in r for r in reasons)
    assert any("statement not analysed: FROBNICATE" in r for r in reasons)          # never silently read as 'no change'
    assert all(b["line"] > 0 and b["text"] for b in bad)


def test_drop_table_dialects_and_unreadable_targets():           # found on spring-petclinic (HSQLDB: DROP TABLE x IF EXISTS)
    o, bad = ops("DROP TABLE vets IF EXISTS;\nDROP TABLE IF EXISTS a, b;\nDROP TABLE owners IF EXISTS CASCADE;\nDROP TABLE some garbage here;")
    assert [x["table"] for x in o] == ["vets", "a", "b", "owners"]
    assert len(bad) == 1 and "DROP TABLE target not analysed" in bad[0]["reason"]            # a bad name is reported, never guessed


def test_alter_table_actions_it_cannot_read_are_reported():
    o, bad = ops("ALTER TABLE t FROBNICATE COLUMN c;")
    assert not o and bad and "ALTER TABLE action not analysed" in bad[0]["reason"]


def test_identifier_and_naming_helpers():
    assert norm_ident('"Public"."Orders"') == "orders" and norm_ident("[dbo].[Orders]") == "orders" and norm_ident("`T`") == "t"
    assert snake("firstName") == "first_name" and snake("userID") == "userid" and snake("OwnerRepository") == "owner_repository"
    assert snake("address2Line") == "address2line" and snake("id") == "id"       # Spring Boot's CamelCaseToUnderscoresNamingStrategy


def test_migration_path_predicates():
    assert is_sql_migration("src/main/resources/db/migration/V1__init.sql") and is_sql_migration("db/migrations/x.sql")
    assert is_sql_migration("schema.sql") and is_sql_migration("V2_1__x.sql") and is_sql_migration("a/R__views.sql")
    assert not is_sql_migration("src/test/resources/db/migration/V1__t.sql") and not is_sql_migration("data.sql")
    assert not is_sql_migration("db/migration/U1__undo.sql") and not is_sql_migration("docs/query.sql")
    assert is_liquibase_path("db/changelog/db.changelog-master.yaml") and is_liquibase_path("a/liquibase/x.xml")
    assert not is_liquibase_path("pom.xml") and not is_liquibase_path("src/test/resources/changelog.xml")


def test_flyway_versions_apply_in_numeric_order_and_replay_to_a_schema():
    e = ex({"db/migration/V10__c.sql": "ALTER TABLE t DROP COLUMN b;", "db/migration/V2__b.sql": "ALTER TABLE t ADD COLUMN b int;",
            "db/migration/V1__a.sql": "CREATE TABLE t (id int PRIMARY KEY, a text);",
            "db/migration/V1_5__a2.sql": "ALTER TABLE t ADD COLUMN c text;"})
    assert [m.version for m in order_migrations(e.migrations)] == ["1", "1.5", "2", "10"]
    assert set(schema_state(e.migrations)["t"]) == {"id", "a", "c"}


def test_sql_files_failing_nothing_but_unknown_statements_do_not_fail_the_scan():
    e = ex({"db/migration/V1__x.sql": "FROBNICATE;\nALTER TABLE t DROP COLUMN c;"})
    assert not e.files_failed
    m = e.migrations[0]
    assert m.tool == "flyway" and m.version == "1" and len(m.ops) == 1 and len(m.unparsed) == 1


# =====================================================================================================================
# Liquibase
# =====================================================================================================================
LB_XML = """<?xml version="1.0"?>
<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">
  <changeSet id="1" author="a">
    <createTable tableName="Events">
      <column name="id" type="BIGINT"><constraints primaryKey="true" nullable="false"/></column>
      <column name="actor" type="VARCHAR(80)"/>
      <column name="payload" type="CLOB"><constraints nullable="false"/></column>
    </createTable>
    <addColumn tableName="events"><column name="ts" type="TIMESTAMP"/><column name="src" type="VARCHAR(5)"><constraints nullable="false"/></column></addColumn>
    <dropColumn tableName="events" columnName="actor"/>
    <renameColumn tableName="events" oldColumnName="payload" newColumnName="body" columnDataType="CLOB"/>
    <modifyDataType tableName="events" columnName="src" newDataType="VARCHAR(50)"/>
    <addNotNullConstraint tableName="events" columnName="ts"/>
    <dropNotNullConstraint tableName="events" columnName="body"/>
    <renameTable oldTableName="events" newTableName="event_log"/>
    <createIndex tableName="event_log" indexName="i"><column name="ts"/></createIndex>
    <sql>ALTER TABLE event_log DROP COLUMN src;</sql>
    <createProcedure>BEGIN NULL; END;</createProcedure>
    <dropTable tableName="old"/>
    <rollback><dropTable tableName="events"/></rollback>
  </changeSet>
</databaseChangeLog>
"""


def test_liquibase_xml_changes():
    out = extract_data_file("r", "s", "db/changelog/001.xml", LB_XML)
    m = out.migrations[0]
    assert m.tool == "liquibase" and not out.files_failed
    seen = [(o["op"], o["table"], o.get("column"), o.get("new_column") or o.get("new_table"), o.get("type"), o.get("nullable")) for o in m.ops]
    assert seen[0][:2] == ("create_table", "events") and set(m.ops[0]["columns"]) == {"id", "actor", "payload"}
    assert m.ops[0]["columns"]["payload"]["nullable"] is False and m.ops[0]["columns"]["id"]["pk"] is True
    assert ("add_column", "events", "ts", None, "timestamp", True) in seen and ("add_column", "events", "src", None, "varchar(5)", False) in seen
    assert ("drop_column", "events", "actor", None, None, None) in seen and ("rename_column", "events", "payload", "body", None, None) in seen
    assert ("alter_column", "events", "src", None, "varchar(50)", None) in seen and ("alter_column", "events", "ts", None, None, False) in seen
    assert ("alter_column", "events", "body", None, None, True) in seen and ("rename_table", "events", None, "event_log", None, None) in seen
    assert ("drop_column", "event_log", "src", None, None, None) in seen and ("drop_table", "old", None, None, None, None) in seen
    assert not any(o["op"] == "drop_table" and o["table"] == "events" for o in m.ops)                  # <rollback> is not forward
    assert [u["reason"] for u in m.unparsed] == ["Liquibase createProcedure is not analysed"]
    assert all(o["line"] > 1 for o in m.ops)


def test_liquibase_yaml_and_json_changelogs():
    y = extract_data_file("r", "s", "db/changelog/002.yaml", """databaseChangeLog:
  - changeSet:
      id: 2
      author: a
      changes:
        - createTable:
            tableName: t
            columns:
              - column: {name: id, type: BIGINT, constraints: {primaryKey: true}}
              - column: {name: c, type: TEXT}
        - dropColumn: {tableName: t, columnName: c}
        - frobnicate: {tableName: t}
""")
    assert [o["op"] for o in y.migrations[0].ops] == ["create_table", "drop_column"]
    assert y.migrations[0].ops[0]["columns"]["id"]["pk"] and "Liquibase change type not analysed: frobnicate" in y.migrations[0].unparsed[0]["reason"]
    j = extract_data_file("r", "s", "db/changelog/003.json", '{"databaseChangeLog": [{"changeSet": {"id": "3", "author": "a", '
                                                              '"changes": [{"dropTable": {"tableName": "t"}}]}}]}')
    assert [o["op"] for o in j.migrations[0].ops] == ["drop_table"]


def test_liquibase_non_changelog_files_are_ignored_and_hostile_xml_is_not_parsed():
    assert not extract_data_file("r", "s", "docs/changelog.yaml", "- version: 1\n  notes: x\n").migrations
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><databaseChangeLog>&b;</databaseChangeLog>'
    out = extract_data_file("r", "s", "db/changelog/x.xml", bomb)
    assert not out.migrations and out.files_failed and "DOCTYPE" in out.files_failed[0]["error"]
    bad = extract_data_file("r", "s", "db/changelog/y.xml", "<databaseChangeLog><changeSet>")
    assert bad.files_failed and "cannot parse migration" in bad.files_failed[0]["error"]


# =====================================================================================================================
# Alembic and Django migrations
# =====================================================================================================================
ALEMBIC = '''import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.create_table("runs", sa.Column("id", sa.BigInteger, primary_key=True), sa.Column("note", sa.String(20), nullable=False))
    op.add_column("runs", sa.Column("score", sa.Integer(), nullable=True, server_default="0"))
    op.drop_column("runs", "legacy")
    op.alter_column("runs", "note", new_column_name="memo", type_=sa.String(40), nullable=True)
    op.rename_table("runs", "run_log")
    with op.batch_alter_table("run_log") as batch_op:
        batch_op.add_column(sa.Column("tag", sa.String(5), nullable=False))
        batch_op.drop_column("score")
    op.execute("ALTER TABLE run_log DROP COLUMN memo")
    op.execute(sa.text("DROP TABLE junk"))
    op.execute(f"DROP TABLE {NAME}")
    op.create_index("ix", "run_log", ["tag"])


def downgrade():
    op.drop_table("run_log")
'''


def test_alembic_upgrade_operations():
    e = ex({"alembic/versions/0002_x.py": ALEMBIC})
    m = e.migrations[0]
    assert m.tool == "alembic" and m.version == "0002" and m.parent == "0001"
    seen = [(o["op"], o["table"], o.get("column"), o.get("new_column") or o.get("new_table"), o.get("type"), o.get("nullable")) for o in m.ops]
    assert m.ops[0]["op"] == "create_table" and m.ops[0]["columns"]["note"] == {"type": "varchar(20)", "nullable": False, "pk": False}
    assert ("add_column", "runs", "score", None, "integer", True) in seen and ("drop_column", "runs", "legacy", None, None, None) in seen
    assert ("rename_column", "runs", "note", "memo", None, None) in seen and ("alter_column", "runs", "memo", None, "varchar(40)", True) in seen
    assert ("rename_table", "runs", None, "run_log", None, None) in seen
    assert ("add_column", "run_log", "tag", None, "varchar(5)", False) in seen and ("drop_column", "run_log", "score", None, None, None) in seen
    assert ("drop_column", "run_log", "memo", None, None, None) in seen and ("drop_table", "junk", None, None, None, None) in seen
    assert not any(o["op"] == "drop_table" and o["table"] == "run_log" for o in m.ops)               # downgrade() is not forward
    assert len(m.unparsed) == 1 and "dynamic SQL" in m.unparsed[0]["reason"]
    assert [o["line"] for o in m.ops] == sorted(o["line"] for o in m.ops)
    assert not e.data_access and not e.data_refs                                                    # upgrade() is a migration, not data access


def test_alembic_revision_chain_orders_branches_and_replays():
    mk = lambda rev, parent, body: f'from alembic import op\nimport sqlalchemy as sa\nrevision = "{rev}"\ndown_revision = {parent!r}\n\ndef upgrade():\n    {body}\n'      # noqa: E731
    e = ex({"v/c.py": mk("c", "b", 'op.drop_column("t", "x")'), "v/a.py": mk("a", None, 'op.create_table("t", sa.Column("id", sa.Integer), sa.Column("x", sa.Text))'),
            "v/b.py": mk("b", "a", 'op.add_column("t", sa.Column("y", sa.Text))')})
    assert [m.version for m in order_migrations(e.migrations)] == ["a", "b", "c"]
    assert set(schema_state(e.migrations)["t"]) == {"id", "y"}


DJ_0001 = '''from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.CreateModel(name="Score", fields=[("id", models.AutoField(primary_key=True)), ("band", models.CharField(max_length=2)),
                                                    ("owner", models.ForeignKey("auth.User", on_delete=models.CASCADE)),
                                                    ("tags", models.ManyToManyField("Tag"))]),
        migrations.CreateModel(name="Legacy", fields=[("code", models.CharField(max_length=3, primary_key=True))], options={"db_table": "legacy_tbl"}),
    ]
'''
DJ_0002 = '''from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("risk", "0001_initial"), ("other", "0009_x")]
    operations = [
        migrations.AddField("score", "note", models.TextField(null=True)),
        migrations.AddField(model_name="score", name="region", field=models.CharField(max_length=4)),
        migrations.AddField(model_name="score", name="team", field=models.ForeignKey("auth.Group", null=True, on_delete=models.SET_NULL)),
        migrations.AlterField(model_name="score", name="band", field=models.CharField(max_length=5, null=True)),
        migrations.RenameField(model_name="score", old_name="note", new_name="remark"),
        migrations.RemoveField(model_name="score", name="region"),
        migrations.RenameModel(old_name="Score", new_name="Rating"),
        migrations.AlterModelTable(name="rating", table="ratings"),
        migrations.DeleteModel(name="Legacy"),
        migrations.RunSQL("ALTER TABLE risk_rating ADD COLUMN extra int"),
        migrations.RunPython(lambda apps, schema: None),
    ]
'''


def test_django_migrations_cover_every_supported_operation():
    e = ex({"risk/migrations/0001_initial.py": DJ_0001, "risk/migrations/0002_more.py": DJ_0002, "risk/migrations/__init__.py": ""})
    m1, m2 = sorted(e.migrations, key=lambda m: m.version)
    assert m1.tool == "django" and m2.parent == "0001_initial"                                       # only this app's dependency
    created = {o["table"]: o for o in m1.ops if o["op"] == "create_table"}
    assert set(created) == {"risk_score", "legacy_tbl", "risk_score_tags"}                           # db_table honoured, M2M link table
    assert set(created["risk_score"]["columns"]) == {"id", "band", "owner_id"} and created["risk_score"]["columns"]["id"]["pk"]
    assert created["risk_score"]["columns"]["owner_id"]["nullable"] is False
    seen = [(o["op"], o["table"], o.get("column"), o.get("new_column") or o.get("new_table"), o.get("nullable")) for o in m2.ops]
    assert ("add_column", "risk_score", "note", None, True) in seen and ("add_column", "risk_score", "region", None, False) in seen
    assert ("add_column", "risk_score", "team_id", None, True) in seen                                # ForeignKey -> <name>_id
    assert ("alter_column", "risk_score", "band", None, True) in seen and ("rename_column", "risk_score", "note", "remark", None) in seen
    assert ("drop_column", "risk_score", "region", None, None) in seen and ("rename_table", "risk_score", None, "risk_rating", None) in seen
    assert ("rename_table", "risk_rating", None, "ratings", None) in seen and ("drop_table", "legacy_tbl", None, None, None) in seen
    assert ("add_column", "risk_rating", "extra", None, True) in seen                                 # RunSQL is parsed
    assert [u["reason"][:9] for u in m2.unparsed] == ["RunPython"]


# =====================================================================================================================
# JPA entities and Spring Data repositories
# =====================================================================================================================
def test_jpa_naming_columns_and_relationships():
    e = jpa('''package com.acme;
import jakarta.persistence.*;
@Entity
public class UserAccount extends Base {
    @Id @GeneratedValue private Long id;
    private String firstName;
    private String userID;
    @Column(name = "TOTAL_AMT", nullable = false) private java.math.BigDecimal total;
    private int visits;
    @Transient private int cache;
    private transient int skipped;
    private static int COUNTER;
    @ManyToOne private Customer owner;
    @ManyToOne @JoinColumn(name = "branch_ref", nullable = false) private Branch branch;
    @OneToMany(mappedBy = "owner") private java.util.List<Order> orders;
    @OneToOne(mappedBy = "account") private Profile profile;
    @ManyToMany @JoinTable(name = "account_tags", joinColumns = @JoinColumn(name = "account_id"),
        inverseJoinColumns = {@JoinColumn(name = "tag_id")}) private java.util.Set<Tag> tags;
    @ManyToMany private java.util.Set<Group> groups;
}''', extra={"src/main/java/com/acme/Customer.java": "package com.acme;\nimport jakarta.persistence.*;\n@Entity public class Customer { @Id private Long customerNo; }\n",
             "src/main/java/com/acme/Base.java": "package com.acme;\nimport jakarta.persistence.*;\n@MappedSuperclass public abstract class Base { private String createdBy; }\n"})
    acc = next(x for x in e.entities if x.name == "UserAccount")
    assert acc.table == "user_account" and acc.table_source == "naming_strategy" and acc.kind == "jpa"
    assert set(acc.columns) == {"id", "created_by", "first_name", "userid", "total_amt", "visits", "owner_customer_no", "branch_ref"}
    assert acc.columns["id"]["pk"] and acc.columns["id"]["nullable"] is False and acc.columns["total_amt"]["nullable"] is False
    assert acc.columns["visits"]["nullable"] is False and acc.columns["first_name"]["nullable"] is True       # primitives are NOT NULL
    assert acc.columns["branch_ref"]["nullable"] is False and acc.columns["owner_customer_no"]["type"] == "fk(Customer)"   # key of the target
    assert acc.join_tables == [{"table": "account_tags", "columns": {"account_id": {"field": "tags", "type": "fk", "nullable": False, "pk": False},
                                                                     "tag_id": {"field": "tags", "type": "fk", "nullable": False, "pk": False}}}]
    assert any(u["kind"] == "default_join_table" and "groups" in u["reason"] for u in e.data_unknowns)
    assert [x.name for x in e.entities] == ["Customer", "UserAccount"]                                      # the mapped superclass maps no table


def test_jpa_table_annotation_embedded_and_inheritance():
    e = jpa('''package com.acme;
import jakarta.persistence.*;
@Embeddable class Money { private java.math.BigDecimal amount; private String currency; }
@Entity @Table(name = "PAYMENTS", schema = "pay") @Inheritance(strategy = InheritanceType.SINGLE_TABLE)
class Payment { @Id private Long id; @Embedded private Money fee; }
''')
    p = e.entities[0]
    assert p.name == "Payment" and p.table == "payments" and p.table_source == "declared"
    assert set(p.columns) == {"id", "amount", "currency"}                                                  # @Embedded columns are merged
    assert [u["kind"] for u in e.data_unknowns] == ["entity_inheritance"]


def test_jpa_entities_in_test_sources_are_ignored_and_data_libraries_reported():
    t = jpa("package a;\nimport jakarta.persistence.*;\n@Entity class T { @Id Long id; }\n", name="src/test/java/a/T.java")
    assert not t.entities
    e = jpa('''package a;
import jakarta.persistence.EntityManager;
import org.springframework.jdbc.core.JdbcTemplate;
class Dao { EntityManager em; JdbcTemplate jdbc; void f() { em.createQuery("x"); } }''')
    kinds_ = sorted(u["reason"].split(" is used")[0] for u in e.data_unknowns if u["kind"] == "data_access_library")
    assert kinds_ == ["JPA EntityManager", "Spring JDBC"]                                                    # never read as 'no data access'


REPO_FILES = {
    "src/main/java/com/acme/Order.java": "package com.acme;\nimport jakarta.persistence.*;\n@Entity @Table(name = \"orders\")\npublic class Order { @Id Long id; String status; }\n",
    "src/main/java/com/acme/Customer.java": "package com.acme;\nimport jakarta.persistence.*;\n@Entity public class Customer { @Id Long id; }\n",
    "src/main/java/com/acme/OrderRepository.java": '''package com.acme;
import java.util.List;
import org.springframework.data.jpa.repository.*;
public interface OrderRepository extends JpaRepository<Order, Long> {
    List<Order> findByStatus(String s);
    long countByStatus(String s);
    boolean existsByStatus(String s);
    void deleteByStatus(String s);
    @Modifying @Query("update Order o set o.status = ?2 where o.id = ?1") int setStatus(Long id, String s);
    @Query("select o from Order o join Customer c on c.id = o.id") List<Order> withCustomers();
    @Query(value = """
        select * from customer c, orders o
        """, nativeQuery = true) List<Order> nativeJoin();
    @Query(value = "delete from orders where status = ?1", nativeQuery = true) void purge(String s);
    Order mystery();
}''',
    "src/main/java/com/acme/OrderService.java": '''package com.acme;
public class OrderService {
    private final OrderRepository orders;
    public OrderService(OrderRepository orders) { this.orders = orders; }
    public void a() { orders.findByStatus("x"); orders.countByStatus("x"); orders.existsByStatus("x"); orders.findById(1L); }
    public void b() { orders.save(new Order()); orders.deleteById(1L); orders.saveAll(java.util.List.of()); }
    public void c() { orders.deleteByStatus("x"); orders.setStatus(1L, "y"); }
    public void d() { orders.withCustomers(); orders.nativeJoin(); orders.purge("x"); }
    public void e() { orders.mystery(); }
}''',
}


def test_spring_data_repository_access_is_classified_by_what_the_method_does():
    e = ex(REPO_FILES)
    acc = {(a.function_id.split("::")[-1], a.entity.split(":")[-1], a.op) for a in e.data_access}
    assert ("OrderService.a", "Order", "read") in acc and ("OrderService.a", "Order", "write") not in acc     # find/count/exists
    assert ("OrderService.b", "Order", "write") in acc and ("OrderService.b", "Order", "read") not in acc     # save/delete/saveAll
    assert ("OrderService.c", "Order", "write") in acc                                                       # derived delete, @Modifying @Query
    assert ("OrderService.d", "Order", "read") in acc and ("OrderService.d", "Order", "write") in acc         # purge is a native delete
    assert ("OrderService.d", "Customer", "read") in acc                                                     # entities named by the query itself
    assert not any(a[0] == "OrderService.e" for a in acc)
    assert any(u["kind"] == "repository_method_unknown" and "mystery" in u["reason"] for u in e.data_unknowns)
    assert e.repositories == [] and e.data_refs == []                                                        # assembly inputs are not retained


def test_a_project_under_a_framework_package_name_keeps_its_repository_calls():   # found on spring-petclinic (org.springframework.samples)
    e = ex({"src/main/java/org/springframework/samples/Pet.java": "package org.springframework.samples;\nimport jakarta.persistence.*;\n"
                                                                  "@Entity public class Pet { @Id Integer id; }\n",
            "src/main/java/org/springframework/samples/PetRepository.java": "package org.springframework.samples;\n"
                                                                            "import org.springframework.data.jpa.repository.JpaRepository;\n"
                                                                            "public interface PetRepository extends JpaRepository<Pet, Integer> {}\n",
            "src/main/java/org/springframework/samples/PetController.java": "package org.springframework.samples;\n"
                                                                            "import org.springframework.web.client.RestTemplate;\n"
                                                                            "class PetController { PetRepository pets; RestTemplate rest;\n"
                                                                            "  void f() { pets.findById(1); pets.save(null); rest.toString(); } }\n"})
    assert {(a.function_id.split("::")[-1], a.op) for a in e.data_access} == {("PetController.f", "read"), ("PetController.f", "write")}


def test_repository_of_an_entity_that_is_not_analysed_is_reported():
    e = ex({"src/main/java/a/R.java": "package a;\nimport org.springframework.data.jpa.repository.*;\ninterface R extends JpaRepository<Ghost, Long> {}\n",
            "src/main/java/a/S.java": "package a;\nclass S { R r; void f() { r.findAll(); } }\n"})
    assert not e.data_access and any(u["kind"] == "repository_entity_unresolved" for u in e.data_unknowns)


def test_custom_hibernate_naming_strategy_is_disclosed():
    e = ex({"src/main/resources/application.properties": "spring.application.name=x\nspring.jpa.hibernate.naming.physical-strategy="
                                                          "org.hibernate.boot.model.naming.PhysicalNamingStrategyStandardImpl\n",
            "src/main/java/a/E.java": "package a;\nimport jakarta.persistence.*;\n@Entity class E { @Id Long id; }\n"})
    assert any(u["kind"] == "custom_naming_strategy" for u in e.data_unknowns)
    ok = ex({"src/main/resources/application.properties": "spring.jpa.hibernate.naming.physical-strategy="
                                                           "org.springframework.boot.orm.jpa.hibernate.SpringPhysicalNamingStrategy\n",
             "src/main/java/a/E.java": "package a;\nimport jakarta.persistence.*;\n@Entity class E { @Id Long id; }\n"})
    assert not any(u["kind"] == "custom_naming_strategy" for u in ok.data_unknowns)


# =====================================================================================================================
# Python ORM models and data access
# =====================================================================================================================
def test_sqlalchemy_declarative_mapped_mixins_and_flask_defaults():
    e = ex({"models.py": '''import sqlalchemy as sa
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, declarative_base
from flask_sqlalchemy import SQLAlchemy

Base = declarative_base()
db = SQLAlchemy()


class Stamped:
    created = Column(sa.DateTime, nullable=False)


class Account(Stamped, Base):
    __tablename__ = "Accounts"
    id = Column(Integer, primary_key=True)
    owner = Column("owner_name", String(50), nullable=False)
    note = Column(String)


class Entry(Base):
    __tablename__ = "entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    memo: Mapped[str] = mapped_column(String(10))
    tag: Mapped[str | None] = mapped_column("tag_name")


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(20))


class Dyn(Base):
    __tablename__ = make_name()
    id = Column(Integer, primary_key=True)
'''})
    by = {x.name: x for x in e.entities}
    assert set(by) == {"Account", "Entry", "AuditLog"}                       # the mixin is folded in; the dynamic table is not mapped
    assert by["Account"].table == "accounts" and set(by["Account"].columns) == {"created", "id", "owner_name", "note"}
    assert by["Account"].columns["owner_name"]["nullable"] is False and by["Account"].columns["id"]["pk"] and by["Account"].columns["note"]["nullable"]
    assert set(by["Entry"].columns) == {"id", "memo", "tag_name"} and by["Entry"].columns["id"]["nullable"] is False
    assert by["Entry"].columns["tag_name"]["nullable"] is True and by["Entry"].columns["memo"]["type"] == "varchar(10)"
    assert by["AuditLog"].table == "audit_log" and by["AuditLog"].table_source == "naming_strategy"          # Flask-SQLAlchemy default
    assert any(u["kind"] == "dynamic_tablename" for u in e.data_unknowns)


def test_sqlmodel_tables_and_non_tables():
    e = ex({"m.py": '''from typing import Optional
from sqlmodel import Field, Relationship, SQLModel


class ItemBase(SQLModel):
    title: str


class Item(ItemBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", nullable=False)
    note: str | None = None
    owner: "User" = Relationship(back_populates="items")
'''})
    assert [x.name for x in e.entities] == ["Item"]
    i = e.entities[0]
    assert i.table == "item" and set(i.columns) == {"title", "id", "owner_id", "note"} and i.columns["id"]["pk"] and i.columns["owner_id"]["nullable"] is False
    assert i.columns["note"]["nullable"] is True


def test_django_models_inheritance_foreign_keys_and_serializers():
    e = ex({"shop/models.py": '''from django.db import models
from rest_framework import serializers


class Stamped(models.Model):
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class Order(Stamped):
    code = models.CharField(max_length=10)
    customer = models.ForeignKey("Customer", on_delete=models.CASCADE, null=True)
    items = models.ManyToManyField("Item")


class Customer(models.Model):
    name = models.CharField(max_length=40, db_column="full_name")

    class Meta:
        db_table = "shop_customers"


class OrderSerializer(serializers.Serializer):
    code = serializers.CharField()
'''})
    by = {x.name: x for x in e.entities}
    assert set(by) == {"Order", "Customer"}                                          # abstract folded in; DRF serializer is not a model
    o, c = by["Order"], by["Customer"]
    assert o.table == "shop_order" and o.table_source == "django_default" and o.app == "shop"
    assert set(o.columns) == {"id", "created", "code", "customer_id"} and o.columns["id"]["pk"] and o.columns["customer_id"]["nullable"]
    assert [t["table"] for t in o.join_tables] == ["shop_order_items"]
    assert c.table == "shop_customers" and c.table_source == "declared" and set(c.columns) == {"id", "full_name"}


PY_MODELS = '''from django.db import models
import sqlalchemy


class Order(models.Model):
    code = models.CharField(max_length=3)

    def mark(self):
        self.save()
        Order.objects.filter(pk=self.pk).update(code="x")

    @classmethod
    def count_all(cls):
        return cls.objects.count()


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
'''

PY_SERVICE = '''from m import Order, Account


def read_django(pk):
    return Order.objects.get(pk=pk)


def write_django():
    Order.objects.create(code="a")
    o = Order(code="b")
    o.save()


def delete_django(pk):
    obj = Order.objects.get(pk=pk)
    obj.delete()


def sa_read(session):
    session.query(Account).filter(Account.id == 1).all()
    session.execute(select(Account.id))
    session.get(Account, 3)


def sa_write(session, a: Account):
    session.add(Account(id=1))
    session.query(Account).filter(Account.id == 2).update({"id": 3})
    session.query(Account).delete()
    session.delete(a)
    session.execute(insert(Account).values(id=4))


def raw(session):
    session.execute("DELETE FROM accounts")
    session.execute(text("select 1"))


def unrelated(d):
    return d.get("key"), sorted(d)
'''


def test_django_self_referential_many_to_many_and_auth_bases():     # found on django-realworld-example-app
    e = ex({"conduit/profiles/models.py": '''from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from polymorphic.models import PolymorphicModel


class Profile(models.Model):
    follows = models.ManyToManyField("self", related_name="followed_by", symmetrical=False)
    favorites = models.ManyToManyField("articles.Article", related_name="fav")


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)


class Doc(PolymorphicModel):
    title = models.CharField(max_length=5)
'''})
    by = {x.name: x for x in e.entities}
    tables = {t["table"]: set(t["columns"]) for t in by["Profile"].join_tables}
    assert tables["profiles_profile_follows"] == {"id", "from_profile_id", "to_profile_id"}        # self-referential
    assert tables["profiles_profile_favorites"] == {"id", "profile_id", "article_id"}
    assert {"password", "last_login", "is_superuser", "email", "id"} == set(by["User"].columns)       # inherited from django.contrib.auth
    assert {t["table"] for t in by["User"].join_tables} == {"profiles_user_groups", "profiles_user_user_permissions"}
    assert [u["kind"] for u in e.data_unknowns] == ["unresolved_base_model"] and "PolymorphicModel" in e.data_unknowns[0]["reason"]
    mig = ex({"app/migrations/0001_initial.py": '''from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.CreateModel(name="Profile", fields=[("id", models.AutoField(primary_key=True)),
        ("follows", models.ManyToManyField(related_name="f", to="app.Profile")), ("fav", models.ManyToManyField(to="app.Article"))])]
'''}).migrations[0]
    through = {o["table"]: set(o["columns"]) for o in mig.ops if o["op"] == "create_table" and o["table"] != "app_profile"}
    assert through == {"app_profile_follows": {"id", "from_profile_id", "to_profile_id"}, "app_profile_fav": {"id", "profile_id", "article_id"}}


def test_python_data_access_references():
    e = ex({"m.py": PY_MODELS, "svc.py": PY_SERVICE})
    assert {x.name for x in e.entities} == {"Order", "Account"}
    refs = {}
    for a in e.data_access:
        refs.setdefault(a.function_id.split("::")[-1], set()).add((a.entity.split(":")[-1], a.op))
    assert refs["read_django"] == {("Order", "read")}
    assert refs["write_django"] == {("Order", "write")}                                    # objects.create, and Model(...).save() through a variable
    assert refs["delete_django"] == {("Order", "read"), ("Order", "write")}               # objects.get, then obj.delete()
    assert refs["sa_read"] == {("Account", "read")}                                        # query(), select(Model.col), session.get(Model, pk)
    assert refs["sa_write"] == {("Account", "read"), ("Account", "write")}                 # add(Model()), query().update/delete, delete(param), insert()
    assert refs["Order.mark"] == {("Order", "write"), ("Order", "read")}                   # self.save(); objects.filter().update()
    assert refs["Order.count_all"] == {("Order", "read")}                                  # cls.objects resolves to the model
    assert "unrelated" not in refs and "raw" not in refs                                   # dict.get / sorted are not data access
    raw = [u for u in e.data_unknowns if u["kind"] == "raw_sql"]
    assert len(raw) == 2 and all("raw" in u["reason"] for u in raw)                        # raw SQL is reported, never ignored


# =====================================================================================================================
# Database identity
# =====================================================================================================================
def test_jdbc_database_identity():
    assert jdbc_database_id("jdbc:postgresql://DB1:5432/Orders?ssl=true") == "postgresql://db1:5432/orders"
    assert jdbc_database_id("jdbc:mysql://h/orders") == "mysql://h/orders"
    assert jdbc_database_id("jdbc:sqlserver://h:1433;databaseName=Ledger;encrypt=true") == "sqlserver://h:1433/ledger"
    assert jdbc_database_id("jdbc:oracle:thin:@//ora:1521/SVC") == "oracle://ora:1521/svc"
    assert jdbc_database_id("jdbc:h2:mem:test") is None and jdbc_database_id("jdbc:hsqldb:mem:x") is None
    assert jdbc_database_id("jdbc:postgresql://${DB_HOST}/orders") is None and jdbc_database_id("not a url") is None


def test_database_comes_from_the_declaration_the_datasource_url_or_is_private():
    cfg = "src/main/resources/application.yml"
    decl = ex({"tia-services.yaml": "service: a\ndatabase: Shared-DB\n", cfg: "spring:\n  datasource:\n    url: jdbc:postgresql://h:5432/other\n"})
    assert decl.service_map["database"] == "shared-db" and decl.service_map["database_source"] == "declared"
    url = ex({cfg: "spring:\n  application:\n    name: a\n  datasource:\n    url: jdbc:postgresql://h:5432/ledger\n"})
    assert url.service_map["database"] == "postgresql://h:5432/ledger" and url.service_map["database_source"] == "datasource_url"
    props = ex({"src/main/resources/application.properties": "spring.datasource.url=jdbc:mysql://m/orders\n"})
    assert props.service_map["database"] == "mysql://m/orders"
    assert "database" not in ex({cfg: "spring:\n  datasource:\n    url: ${DB_URL}\n"}).service_map


def _service_with_orders(name, database):
    files = {"m.py": "from sqlalchemy import Column, Integer, String\nclass Order(Base):\n    __tablename__ = 'orders'\n"
                     "    id = Column(Integer, primary_key=True)\n    status = Column(String)\n",
             "tia-services.yaml": f"service: {name}\n" + (f"database: {database}\n" if database else "")}
    return ex(files, repo=name)


def test_tables_with_the_same_name_are_linked_only_through_a_shared_database():
    shared = build_graph({"a": _service_with_orders("a", "pg1"), "b": _service_with_orders("b", "pg1")})
    assert "table:pg1:orders" in shared.nodes and len([n for n in shared.nodes if n.startswith("table:")]) == 1
    assert {n for n, e in shared.impacted_by("table:pg1:orders")} == {"a:entity:Order", "b:entity:Order"}
    private = build_graph({"a": _service_with_orders("a", None), "b": _service_with_orders("b", None)})
    assert {n for n in private.nodes if n.startswith("table:")} == {"table:private:a:orders", "table:private:b:orders"}
    other = build_graph({"a": _service_with_orders("a", "pg1"), "b": _service_with_orders("b", "pg2")})
    assert len([n for n in other.nodes if n.startswith("table:")]) == 2                    # names alone never link services


# =====================================================================================================================
# Schema change classification
# =====================================================================================================================
def test_type_widening_is_only_claimed_when_provable():
    assert type_widened("varchar(10)", "varchar(20)") and type_widened("varchar(10)", "text") and type_widened("int", "bigint")
    assert type_widened("smallint", "integer") and type_widened("numeric(10,2)", "numeric(12,2)") and type_widened("integer", "integer")
    assert not type_widened("varchar(20)", "varchar(10)") and not type_widened("bigint", "int") and not type_widened("text", "varchar(10)")
    assert not type_widened("numeric(10,2)", "numeric(10,4)") and not type_widened("int", "varchar(10)") and not type_widened(None, "int")
    assert not type_widened("varchar(10)", "char(20)") and not type_widened("date", "timestamp")


def test_sqlalchemy_types_are_normalised_so_widening_is_recognised_across_tools():
    from tia.extract_data import sa_type
    assert [sa_type(x) for x in ("sa.String(50)", "String", "sqlalchemy.Integer()", "sa.BigInteger", "sa.Numeric(10, 2)", "sa.DateTime",
                                 "postgresql.JSONB")] == ["varchar(50)", "varchar", "integer", "bigint", "numeric(10,2)", "timestamp", "jsonb"]
    mk = lambda body: f'from alembic import op\nimport sqlalchemy as sa\nrevision = "{abs(hash(body)) % 999}"\ndown_revision = None\n\ndef upgrade():\n    {body}\n'   # noqa: E731
    base = ex({"v/a.py": 'from alembic import op\nimport sqlalchemy as sa\nrevision = "a"\ndown_revision = None\n\ndef upgrade():\n'
                         '    op.create_table("t", sa.Column("c", sa.String(20)), sa.Column("n", sa.Integer))\n'})
    head = ex({"v/a.py": 'from alembic import op\nimport sqlalchemy as sa\nrevision = "a"\ndown_revision = None\n\ndef upgrade():\n'
                         '    op.create_table("t", sa.Column("c", sa.String(20)), sa.Column("n", sa.Integer))\n',
               "v/b.py": 'from alembic import op\nimport sqlalchemy as sa\nrevision = "b"\ndown_revision = "a"\n\ndef upgrade():\n'
                         '    op.alter_column("t", "c", type_=sa.String(40))\n    op.alter_column("t", "n", type_=sa.String(10))\n'})
    got = {c["column"]: c["kind"] for c in diff_data(base, head)[0]}
    assert got == {"c": "column_type_widened", "n": "column_type_changed"}


def test_diff_data_classifies_only_what_the_head_adds():
    base = ex({"db/migration/V1__a.sql": "CREATE TABLE t (id int PRIMARY KEY, a varchar(10) NOT NULL, b text);"})
    head = ex({"db/migration/V1__a.sql": "CREATE TABLE t (id int PRIMARY KEY, a varchar(10) NOT NULL, b text);",
               "db/migration/V2__b.sql": """ALTER TABLE t ADD COLUMN c int; ALTER TABLE t ADD COLUMN d int NOT NULL;
               ALTER TABLE t ADD COLUMN e int NOT NULL DEFAULT 0; ALTER TABLE t ALTER COLUMN a TYPE varchar(20);
               ALTER TABLE t ALTER COLUMN b TYPE int; ALTER TABLE t ALTER COLUMN b SET NOT NULL;
               ALTER TABLE t ALTER COLUMN a DROP NOT NULL; ALTER TABLE t DROP COLUMN c; ALTER TABLE t RENAME COLUMN a TO aa;
               ALTER TABLE t RENAME TO u; CREATE TABLE v (id int); DROP TABLE v;"""})
    changes, changed = diff_data(base, head)
    got = {c["kind"]: c for c in changes}
    assert [m.file for m in changed] == ["db/migration/V2__b.sql"]
    assert not got["column_added"]["breaking"] and not got["column_type_widened"]["breaking"] and not got["column_made_nullable"]["breaking"]
    assert got["column_added_required"]["breaking"] and got["column_added_required"]["seeds"] == [{"node": "table:private:svc:t", "access": "write"}]
    assert got["column_type_changed"]["breaking"] and "text -> int" in got["column_type_changed"]["detail"]
    assert got["column_made_required"]["seeds"][0]["access"] == "write"
    assert got["column_dropped"]["seeds"] == [{"node": "col:private:svc:t.c", "access": None}]
    assert got["column_renamed"]["breaking"] and got["table_renamed"]["breaking"] and got["table_dropped"]["breaking"] and got["table_added"]["seeds"] == []
    same, none = diff_data(head, head)
    assert same == [] and none == []                                                                    # nothing new, nothing reported


# =====================================================================================================================
# The graph
# =====================================================================================================================
def test_data_graph_for_the_landscape(dfx):
    exs = {n: extract_commit(n, p, "main", None)[0] for n, p in dfx["paths"].items()}
    g = build_graph(exs)
    shared = "table:ledger-db:accounts"
    assert g.nodes[shared]["type"] == "table" and g.nodes[shared]["database"] == "ledger-db"
    mapping = {n for n, e in g.impacted_by(shared) if e.rel == "MAPS_TO"}
    assert mapping == {"ledger-service:entity:Account", "billing-service:entity:BillingAccount", "statements-service:entity:Account"}
    col = "col:ledger-db:accounts.status"
    assert {n for n, e in g.impacted_by(col) if e.rel == "MAPS_COLUMN"} == mapping                       # all three map the column
    assert {n for n, _ in g.impacted_by("col:ledger-db:accounts.owner_name")} == {"ledger-service:entity:Account"}
    assert "col:ledger-db:accounts.legacy_flag" in g.nodes and not g.impacted_by("col:ledger-db:accounts.legacy_flag")   # migration-only column
    readers = {g.nodes[n]["name"] for n, e in g.impacted_by("billing-service:entity:BillingAccount") if e.rel == "READS"}
    writers = {g.nodes[n]["name"] for n, e in g.impacted_by("billing-service:entity:BillingAccount") if e.rel == "WRITES"}
    assert readers == {"ChargeService.charge"} and writers == {"ChargeService.charge"}
    assert {e.rel for e in g.edges if e.src.startswith("mig:")} == {"MIGRATES"}
    assert all(e.status == "source_confirmed" for e in g.edges if e.rel in ("MAPS_TO", "MAPS_COLUMN", "READS", "WRITES", "MIGRATES"))
    risk = "col:private:risk-service:risk_score.band"
    assert risk in g.nodes and {n for n, _ in g.impacted_by(risk)} == {"risk-service:entity:Score"}      # Django: app_model table
    assert "col:private:audit-service:audit_events.actor" in g.nodes                                    # Liquibase + entity


def test_entities_agree_with_the_schema_their_migrations_build(dfx):
    """The two independent extractors cross-check each other: an entity never maps a column its own repository's migrations did not
    create (and, where an entity maps the whole table, it maps every column)."""
    for name, path in dfx["paths"].items():
        e, _ = extract_commit(name, path, "main", None)
        state = schema_state(e.migrations)
        for ent in e.entities:
            if ent.table not in state:
                continue                                                      # shared tables owned by another repository
            assert set(ent.columns) <= set(state[ent.table]), (name, ent.name)
    r, _ = extract_commit("risk-service", dfx["paths"]["risk-service"], "main", None)
    assert set(r.entities[0].columns) == set(schema_state(r.migrations)["risk_score"])           # Django: whole table
    s, _ = extract_commit("statements-service", dfx["paths"]["statements-service"], "main", None)
    snap = next(x for x in s.entities if x.name == "StatementSnapshot")
    assert set(snap.columns) == set(schema_state(s.migrations)["statement_snapshots"])           # SQLAlchemy + Alembic: whole table


def test_graph_roundtrip_keeps_the_data_layer(dfx):
    from tia.graph import Graph
    exs = {n: extract_commit(n, p, "main", None)[0] for n, p in dfx["paths"].items()}
    g = build_graph(exs)
    g2 = Graph.from_dict(g.to_dict())
    assert g2.to_dict() == g.to_dict() and {n for n, _ in g2.impacted_by("table:ledger-db:accounts")} == {n for n, _ in g.impacted_by("table:ledger-db:accounts")}


def test_extracts_roundtrip_through_storage(dfx):
    store = fresh_store()
    e1, _ = extract_commit("ledger-service", dfx["paths"]["ledger-service"], "main", store)
    store.put_extract(e1)
    back = store.get_extract("ledger-service", e1.commit, EXTRACTOR_VERSION)
    assert back.to_dict() == e1.to_dict() and back.entities and back.migrations and back.data_access


# =====================================================================================================================
# Impact analysis (AC-024)
# =====================================================================================================================
def test_ac024_dropped_column_is_reported_for_every_service_that_maps_it(dfx):
    r = run(dfx, "ledger-service", "drop_status")
    assert r["change"]["change_type"] == "data_change"
    comp = next(c for c in r["change"]["changed_components"] if c["type"] == "db_column")
    assert comp["name"] == "accounts.status" and comp["change"] == "column_dropped" and comp["breaking"] and comp["database"] == "ledger-db"
    assert [m["entity"] for m in comp["mapped_by"]] == ["billing-service:entity:BillingAccount", "ledger-service:entity:Account",
                                                         "statements-service:entity:Account"]
    assert {m["repo"] for m in comp["mapped_by"]} == {"billing-service", "ledger-service", "statements-service"}
    ents = {f["component"]: f for f in r["findings"] if f["node_type"] == "entity"}
    assert set(ents) == {"billing-service", "statements-service", "ledger-service"}
    for svc in ("billing-service", "statements-service"):
        f = ents[svc]
        assert f["impact_type"] == "data" and f["change_nature"] == "breaking" and f["evidence_status"] == "source_confirmed"
        assert f["severity"] == "critical" and f["severity_rule"] == "SEV-BREAKING-CRITICAL" and f["material"]
        assert [p["type"] for p in f["path"]] == ["column", "entity"] and [e["rel"] for e in f["path_edges"]] == ["MAPS_COLUMN"]
        assert f["path"][0]["repo"] == "ledger-service"                                          # the column belongs to the repository that owns the table
        assert f["evidence"]["file"] and f["evidence"]["line"] > 0 and f["data"]["database"] == "ledger-db"
        assert any("declared in tia-services.yaml" in lim for lim in f["limitations"])
    assert {a["function"] for a in ents["billing-service"]["data"]["accessors"]} == {"ChargeService.charge"}
    assert {(a["function"], a["op"]) for a in ents["statements-service"]["data"]["accessors"]} == {("load_statement", "read")}
    assert "billing-service" in ents["billing-service"]["reason"] and "accounts.status" in ents["billing-service"]["reason"]


def test_data_impact_propagates_through_endpoints_to_api_consumers(dfx):
    r = run(dfx, "ledger-service", "drop_status")
    eps = {(f["component"], f["node"]) for f in r["findings"] if f["node_type"] == "endpoint"}
    assert eps == {("billing-service", "billing-service:POST /api/billing/charge"), ("statements-service", "statements-service:GET /statements/{}")}
    sites = [f for f in r["findings"] if f["component"] == "portal-bff"]
    assert len(sites) == 2 and all(f["impact_type"] == "transitive" and f["severity"] == "critical" for f in sites)
    for f in sites:
        assert [p["type"] for p in f["path"]] == ["column", "entity", "function", "function", "endpoint", "callsite"]
        assert [e["rel"] for e in f["path_edges"]] == ["MAPS_COLUMN", "READS", "CALLS", "HANDLES", "CONSUMES_API"]
        assert f["evidence"]["file"] == "portal.py" and f["resolution_strategy"] == "R2"


def test_the_changed_repository_is_told_what_its_own_migration_breaks(dfx):
    broken = run(dfx, "ledger-service", "drop_status")
    own = next(f for f in broken["findings"] if f["node_type"] == "entity" and f["component"] == "ledger-service")
    assert own["severity"] == "high" and own["severity_rule"] == "SEV-DATA-MAPPING-BROKEN" and own["material"]
    assert {a["function"] for a in own["data"]["accessors"]} == {"AccountService.get", "AccountService.open", "AccountService.post"}
    fixed = run(dfx, "ledger-service", "drop_status_fixed")           # the entity no longer maps the column in the same change
    own = next(f for f in fixed["findings"] if f["node_type"] == "entity" and f["component"] == "ledger-service")
    assert own["severity"] == "low" and own["severity_rule"] == "SEV-DATA-UPDATED" and not own["material"]
    assert "updated in the same change" in own["reason"]
    assert {f["component"] for f in fixed["findings"] if f["severity"] == "critical"} >= {"billing-service", "statements-service", "portal-bff"}


def test_a_new_required_column_only_breaks_the_services_that_write(dfx):
    r = run(dfx, "ledger-service", "add_required")
    comp = next(c for c in r["change"]["changed_components"] if c["change"] == "column_added_required")
    assert comp["breaking"] and comp["name"] == "accounts.branch"
    cross = {f["component"] for f in r["findings"] if f["severity"] == "critical"}
    assert cross == {"billing-service", "portal-bff"}                                      # statements-service only reads accounts
    assert not any(f["component"] == "statements-service" for f in r["findings"])
    ents = [f for f in r["findings"] if f["node_type"] == "entity"]
    assert {f["component"] for f in ents} == {"billing-service", "ledger-service"}
    assert all(a["op"] == "write" for f in ents for a in f["data"]["accessors"])


def test_type_change_only_affects_services_that_map_that_column(dfx):
    narrow = run(dfx, "ledger-service", "narrow")
    comp = next(c for c in narrow["change"]["changed_components"] if c["type"] == "db_column")
    assert comp["change"] == "column_type_changed" and comp["breaking"] and [m["entity"] for m in comp["mapped_by"]] == ["ledger-service:entity:Account"]
    assert comps(narrow) == {"ledger-service"}                                              # billing/statements map no owner_name
    own = next(f for f in narrow["findings"] if f["node_type"] == "entity")
    assert own["severity_rule"] == "SEV-LOCAL" and any("nullability compatibility" in x for x in own["limitations"])


def test_additive_and_widening_changes_report_nothing_downstream(dfx):
    for branch in ("widen", "add_optional"):
        r = run(dfx, "ledger-service", branch)
        assert r["findings"] == [], branch
        assert r["change"]["change_type"] == "data_change"
        assert all(not c.get("breaking") for c in r["change"]["changed_components"] if c["type"].startswith("db_"))
        assert "not proof" in r["summary"]["message"]
    assert {c["change"] for c in run(dfx, "ledger-service", "widen")["change"]["changed_components"]} == {"column_type_widened"}


def test_renaming_a_table_breaks_every_entity_that_maps_it(dfx):
    r = run(dfx, "ledger-service", "rename_table")
    comp = next(c for c in r["change"]["changed_components"] if c["type"] == "db_table")
    assert comp["change"] == "table_renamed" and "customer_accounts" in comp["detail"] and len(comp["mapped_by"]) == 3
    assert {f["component"] for f in r["findings"] if f["node_type"] == "entity"} == {"billing-service", "statements-service", "ledger-service"}
    assert {f["component"] for f in r["findings"] if f["severity"] == "critical"} >= {"portal-bff"}


def test_a_column_nothing_maps_is_reported_as_unmapped_not_as_clean(dfx):
    r = run(dfx, "ledger-service", "drop_legacy")
    assert r["findings"] == []
    u = next(u for u in r["unknowns"] if u["area"] == "data_change_not_mapped")
    assert u["count"] == 1 and "raw SQL" in u["reason"] and not u["affects_confidence"]
    assert "not proof" in r["summary"]["message"]


def test_statements_that_cannot_be_read_are_unknowns_and_the_readable_ones_still_count(dfx):
    r = run(dfx, "ledger-service", "unparsed")
    assert r["status"] == "complete_with_gaps" and {f["component"] for f in r["findings"]} >= {"billing-service", "portal-bff"}
    u = next(u for u in r["unknowns"] if u["area"] == "unparsed_migration_statements")
    assert u["affects_confidence"] and u["items"][0]["file"].endswith("V2__change.sql") and "procedural SQL" in u["items"][0]["reason"]


def test_a_data_only_migration_changes_no_structure(dfx):
    r = run(dfx, "ledger-service", "data_only")
    assert r["findings"] == [] and r["status"] == "complete" and r["change"]["change_type"] != "data_change"
    assert not [c for c in r["change"]["changed_components"] if c["type"].startswith("db_")]


def test_migrations_are_not_double_counted_as_file_level_changes(dfx):
    r = run(dfx, "ledger-service", "drop_status")
    assert not [c for c in r["change"]["changed_components"] if c["change"] == "file_level"]
    assert not any(u["area"] == "file_level_analysis" for u in r["unknowns"])


@pytest.mark.parametrize("target,branch,entity,rule,table_col", [
    ("statements-service", "alembic_drop", "StatementSnapshot", "SEV-DATA-MAPPING-BROKEN", "statement_snapshots.rendered"),    # Alembic batch
    ("risk-service", "drop_band", "Score", "SEV-DATA-MAPPING-BROKEN", "risk_score.band"),                                      # Django migration
    ("audit-service", "drop_actor", "AuditEvent", "SEV-DATA-MAPPING-BROKEN", "audit_events.actor"),                           # Liquibase YAML
])
def test_each_migration_tool_drives_data_impact(dfx, target, branch, entity, rule, table_col):
    r = run(dfx, target, branch)
    comp = next(c for c in r["change"]["changed_components"] if c["type"] == "db_column")
    assert comp["name"] == table_col and comp["change"] == "column_dropped"
    ent = next(f for f in r["findings"] if f["node_type"] == "entity")
    assert ent["data"]["entity"] == entity and ent["severity_rule"] == rule and ent["component"] == target
    assert ent["data"]["accessors"]


def test_unknowns_say_when_database_identity_or_data_access_is_incomplete(dfx):
    r = run(dfx, "ledger-service", "drop_status")
    u = next(u for u in r["unknowns"] if u["area"] == "database_identity")
    assert {i["service"] for i in u["items"]} == {"risk-service", "audit-service"} and u["affects_confidence"]
    assert r["coverage"]["data"]["databases"] == ["ledger-db", "private:audit-service", "private:risk-service"]
    assert r["coverage"]["data"]["entities"] == 8 and r["coverage"]["data"]["migrations"] == 5


def test_data_analysis_is_deterministic_and_reproducible(dfx):
    a, b = run(dfx, "ledger-service", "drop_status"), run(dfx, "ledger-service", "drop_status")
    for rep in (a, b):
        rep.pop("generated_at")
    assert a == b and a["evidence_snapshot_id"].startswith("snap-")
    store = fresh_store()
    stored = run(dfx, "ledger-service", "drop_status", store)
    again = run(dfx, "ledger-service", "drop_status", store)
    assert {f["id"]: f["severity"] for f in stored["findings"]} == {f["id"]: f["severity"] for f in again["findings"]}
    assert store.get_extract("ledger-service", stored["change"]["base_revision"], EXTRACTOR_VERSION).entities


def test_incremental_extraction_equals_a_full_rescan_for_the_data_layer(dfx):
    p, store = dfx["paths"]["ledger-service"], fresh_store()
    extract_commit("ledger-service", p, "main", store)
    head = gitutil.resolve_rev(p, "drop_status_fixed")                          # a migration is added and one entity changes
    inc, stats = extract_commit("ledger-service", p, head, store)
    assert stats["parsed"] == 2 and stats["reused"] == stats["files"] - 2
    full = extract_repo("ledger-service", head, gitutil.read_tree(p, head))
    assert inc.to_dict() == full.to_dict() and len(inc.migrations) == 2
    gi, gf = build_graph({"ledger-service": inc}), build_graph({"ledger-service": full})
    assert gi.to_dict() == gf.to_dict()


# =====================================================================================================================
# Reporting
# =====================================================================================================================
def test_comment_lists_the_data_change_and_the_services_it_reaches(dfx):
    r = run(dfx, "ledger-service", "drop_status")
    c = render_comment(r)
    assert "change type: **data_change**" in c and "**Data changes**" in c and "BREAKING: column accounts.status dropped" in c
    assert "billing-service" in c and "statements-service" in c and "CRITICAL" in c
    assert "database" in c and c.count("<!-- tia-analysis -->") == 1 and "does not block merge" in c
    quiet = render_comment(run(dfx, "ledger-service", "add_optional"))
    assert "Data changes" not in quiet                                       # additive changes are not warnings


def test_redaction_hides_data_findings_in_repositories_the_audience_cannot_read(dfx):
    r = run(dfx, "ledger-service", "drop_status")
    red = redact(r, ["ledger-service", "billing-service"])
    assert {f["component"] for f in red["findings"]} <= {"ledger-service", "billing-service"} and red["redacted"]["findings"] > 0
    blob = str(red)
    assert "statements-service" not in blob and "portal-bff" not in blob
    u = next(u for u in red["unknowns"] if u["area"] == "database_identity")
    assert u["items"] == [] and u["redacted_items"] == 2                      # services of unreadable repositories are not named


def test_data_findings_over_the_api(dfx, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    for n, p in dfx["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    rep = c.post("/api/v1/impact-analyses", json={"target": "ledger-service", "head": "drop_status", "base": "main"}).json()["report"]
    assert rep["change"]["change_type"] == "data_change" and {"billing-service", "statements-service", "portal-bff"} <= comps(rep)
    got = c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}").json()
    assert got["findings"] == rep["findings"]
    comment = c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}/comment").text
    assert "**Data changes**" in comment and "billing-service" in comment
    fn = c.get("/api/v1/graph", params={"level": "function"}).json()
    assert {"entity", "table", "migration"} <= {n["type"] for n in fn["nodes"]}                        # the data layer is in the graph view
    assert "column" not in {n["type"] for n in fn["nodes"]}                                           # columns appear when searched for
    assert {e["rel"] for e in fn["edges"]} >= {"MAPS_TO", "READS", "WRITES", "MIGRATES"}
    assert next(n for n in fn["nodes"] if n["id"] == "table:ledger-db:accounts")["label"] == "accounts"
    q = c.get("/api/v1/graph", params={"level": "function", "q": "accounts.status"}).json()
    assert {n["type"] for n in q["nodes"]} == {"column", "entity"} and {e["rel"] for e in q["edges"]} == {"MAPS_COLUMN"}
    assert {n["label"] for n in q["nodes"] if n["type"] == "entity"} == {"Account", "BillingAccount"}                # the entities mapping it
    ov = c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}/graph").json()
    assert {"column", "entity", "endpoint", "callsite"} <= {n["type"] for n in ov["nodes"]}             # the overlay draws the whole path
    assert next(n for n in ov["nodes"] if n["type"] == "column")["role"] == "changed"
