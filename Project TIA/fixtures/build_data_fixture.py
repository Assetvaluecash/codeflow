"""Builds the data-layer landscape (real git repos) used by the ORM / migration / data-impact tests.

ledger-service      Java + JPA + Flyway. Owns database 'ledger-db' (accounts, ledger_entries); Spring Data repositories;
                    REST endpoints. PR branches change the schema: drop / rename / retype / require columns.
billing-service     Java + JPA, shares 'ledger-db': maps the accounts table (status, balance) and WRITES it; REST endpoint.
statements-service  Python + FastAPI + SQLAlchemy + Alembic, shares 'ledger-db': READS accounts and ledger_entries and owns
                    statement_snapshots (Alembic); REST endpoints.
risk-service        Python + Django models and Django migrations, no declared database (private).
audit-service       Java + JPA + Liquibase (XML changelog, YAML changelog on a branch), no declared database (private).
portal-bff          Python consumer of billing-service and statements-service (config keys -> service map).
"""
from __future__ import annotations

from pathlib import Path

from fixtures.build_fixture import _commit, _run, _write


def _j(path: str) -> str:
    return "src/main/java/com/acme/" + path


V1_LEDGER = '''CREATE TABLE accounts (
    id BIGSERIAL PRIMARY KEY,
    owner_name VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    balance NUMERIC(14,2) NOT NULL DEFAULT 0,
    legacy_flag BOOLEAN
);

CREATE TABLE ledger_entries (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES accounts(id),
    amount NUMERIC(14,2) NOT NULL,
    memo VARCHAR(200)
);
'''

LEDGER_ACCOUNT = '''package com.acme.ledger;

import jakarta.persistence.*;
import java.math.BigDecimal;

@Entity
@Table(name = "accounts")
public class Account {
    @Id
    @GeneratedValue
    private Long id;

    @Column(name = "owner_name", nullable = false)
    private String ownerName;

    private String status;

    private BigDecimal balance;

    public Long getId() {
        return id;
    }
}
'''

LEDGER_ENTRY = '''package com.acme.ledger;

import jakarta.persistence.*;
import java.math.BigDecimal;

@Entity
@Table(name = "ledger_entries")
public class Entry {
    @Id
    @GeneratedValue
    private Long id;

    @ManyToOne
    @JoinColumn(name = "account_id")
    private Account account;

    private BigDecimal amount;

    private String memo;
}
'''

LEDGER_ACCOUNT_REPO = '''package com.acme.ledger;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;

public interface AccountRepository extends JpaRepository<Account, Long> {
    List<Account> findByStatus(String status);

    @Modifying
    @Query("update Account a set a.balance = ?2 where a.id = ?1")
    int updateBalance(Long id, java.math.BigDecimal balance);
}
'''

LEDGER_ENTRY_REPO = '''package com.acme.ledger;

import org.springframework.data.jpa.repository.JpaRepository;

public interface EntryRepository extends JpaRepository<Entry, Long> {
}
'''

LEDGER_SERVICE = '''package com.acme.ledger;

import java.math.BigDecimal;
import org.springframework.stereotype.Service;

@Service
public class AccountService {
    private final AccountRepository accounts;
    private final EntryRepository entries;

    public AccountService(AccountRepository accounts, EntryRepository entries) {
        this.accounts = accounts;
        this.entries = entries;
    }

    public Account get(Long id) {
        return accounts.findById(id).orElseThrow();
    }

    public Account open(Account a) {
        return accounts.save(a);
    }

    public void post(Long id, BigDecimal amount) {
        entries.save(new Entry());
        accounts.updateBalance(id, amount);
    }
}
'''

LEDGER_CONTROLLER = '''package com.acme.ledger;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/accounts")
public class AccountController {
    private final AccountService service;

    public AccountController(AccountService service) {
        this.service = service;
    }

    @GetMapping("/{id}")
    public Account getAccount(@PathVariable Long id) {
        return service.get(id);
    }

    @PostMapping
    public Account openAccount(@RequestBody Account account) {
        return service.open(account);
    }
}
'''

LEDGER_TEST = '''package com.acme.ledger;

import org.junit.jupiter.api.Test;

class AccountServiceTest {
    @Test
    void getAccountReadsIt() {
        AccountService service = new AccountService(null, null);
        service.get(1L);
    }
}
'''

BILLING_ACCOUNT = '''package com.acme.billing;

import jakarta.persistence.*;
import java.math.BigDecimal;

@Entity
@Table(name = "accounts")
public class BillingAccount {
    @Id
    private Long id;

    private String status;

    private BigDecimal balance;
}
'''

BILLING_REPO = '''package com.acme.billing;

import org.springframework.data.repository.CrudRepository;

public interface BillingAccountRepository extends CrudRepository<BillingAccount, Long> {
}
'''

BILLING_SERVICE = '''package com.acme.billing;

import java.math.BigDecimal;
import org.springframework.stereotype.Service;

@Service
public class ChargeService {
    private final BillingAccountRepository accounts;

    public ChargeService(BillingAccountRepository accounts) {
        this.accounts = accounts;
    }

    public void charge(Long id, BigDecimal amount) {
        BillingAccount account = accounts.findById(id).orElseThrow();
        accounts.save(account);
    }
}
'''

BILLING_CONTROLLER = '''package com.acme.billing;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/billing")
public class BillingController {
    private final ChargeService charges;

    public BillingController(ChargeService charges) {
        this.charges = charges;
    }

    @PostMapping("/charge")
    public void postCharge(@RequestBody ChargeRequest request) {
        charges.charge(request.accountId(), request.amount());
    }
}
'''

CHARGE_REQUEST = '''package com.acme.billing;

public record ChargeRequest(Long accountId, java.math.BigDecimal amount) {
}
'''

STATEMENTS_MODELS = '''from sqlalchemy import BigInteger, Column, ForeignKey, Numeric, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Account(Base):
    __tablename__ = "accounts"
    id = Column(BigInteger, primary_key=True)
    status = Column(String(20), nullable=False)
    balance = Column(Numeric(14, 2), nullable=False)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id = Column(BigInteger, primary_key=True)
    account_id = Column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)


class StatementSnapshot(Base):
    __tablename__ = "statement_snapshots"
    id = Column(BigInteger, primary_key=True)
    account_id = Column(BigInteger, nullable=False)
    rendered = Column(String, nullable=True)
'''

STATEMENTS_APP = '''from fastapi import FastAPI

from db import SessionLocal
from models import Account, LedgerEntry, StatementSnapshot

app = FastAPI()


def load_statement(session, account_id):
    account = session.query(Account).filter(Account.id == account_id).one()
    rows = session.query(LedgerEntry).filter(LedgerEntry.account_id == account_id).all()
    return account, rows


def save_snapshot(session, account_id, text):
    session.add(StatementSnapshot(account_id=account_id, rendered=text))
    session.commit()


@app.get("/statements/{account_id}")
def get_statement(account_id: int):
    with SessionLocal() as session:
        account, rows = load_statement(session, account_id)
        return {"balance": str(account.balance), "entries": len(rows)}


@app.post("/statements/{account_id}/snapshot")
def create_snapshot(account_id: int):
    with SessionLocal() as session:
        save_snapshot(session, account_id, "x")
'''

STATEMENTS_DB = '''from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SessionLocal = sessionmaker(bind=create_engine("postgresql://ledger-db/ledger"))
'''

ALEMBIC_0001 = '''import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.create_table(
        "statement_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("rendered", sa.String(), nullable=True),
    )


def downgrade():
    op.drop_table("statement_snapshots")
'''

ALEMBIC_0002 = '''from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    with op.batch_alter_table("statement_snapshots") as batch_op:
        batch_op.drop_column("rendered")


def downgrade():
    pass
'''

RISK_MODELS = '''from django.db import models


class Score(models.Model):
    customer_id = models.BigIntegerField()
    value = models.IntegerField()
    band = models.CharField(max_length=2)
'''

RISK_SERVICES = '''from .models import Score


def current_band(customer_id):
    return Score.objects.get(customer_id=customer_id).band


def record(customer_id, value):
    Score.objects.create(customer_id=customer_id, value=value, band="A")
'''

RISK_0001 = '''from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Score",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("customer_id", models.BigIntegerField()),
                ("value", models.IntegerField()),
                ("band", models.CharField(max_length=2)),
            ],
        ),
    ]
'''

RISK_0002 = '''from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("risk", "0001_initial")]
    operations = [
        migrations.RemoveField(model_name="score", name="band"),
    ]
'''

AUDIT_EVENT = '''package com.acme.audit;

import jakarta.persistence.*;

@Entity
@Table(name = "audit_events")
public class AuditEvent {
    @Id
    private Long id;

    private String actor;

    private String payload;
}
'''

AUDIT_REPO = '''package com.acme.audit;

import org.springframework.data.jpa.repository.JpaRepository;

public interface AuditRepository extends JpaRepository<AuditEvent, Long> {
}
'''

AUDIT_SERVICE = '''package com.acme.audit;

import org.springframework.stereotype.Service;

@Service
public class AuditService {
    private final AuditRepository events;

    public AuditService(AuditRepository events) {
        this.events = events;
    }

    public void record(AuditEvent e) {
        events.save(e);
    }
}
'''

LIQUIBASE_MASTER = '''databaseChangeLog:
  - include:
      file: changes/001-init.xml
      relativeToChangelogFile: true
'''

LIQUIBASE_001 = '''<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">
  <changeSet id="1" author="audit">
    <createTable tableName="audit_events">
      <column name="id" type="BIGINT">
        <constraints primaryKey="true" nullable="false"/>
      </column>
      <column name="actor" type="VARCHAR(80)"/>
      <column name="payload" type="CLOB"/>
    </createTable>
  </changeSet>
</databaseChangeLog>
'''

LIQUIBASE_002 = '''databaseChangeLog:
  - changeSet:
      id: 2
      author: audit
      changes:
        - dropColumn:
            tableName: audit_events
            columnName: actor
'''

PORTAL = '''import os

import requests

STATEMENTS_URL = os.getenv("STATEMENTS_URL")
BILLING_URL = os.getenv("BILLING_URL")


def statement_for(account_id):
    return requests.get(f"{STATEMENTS_URL}/statements/{account_id}", timeout=3).json()


def charge(account_id, amount):
    return requests.post(f"{BILLING_URL}/api/billing/charge", json={"accountId": account_id, "amount": amount}, timeout=3)
'''


def build(dest: str) -> dict:
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    out = {"paths": {}, "shas": {}}

    def repo(name, files):
        p = root / name
        p.mkdir(parents=True, exist_ok=True)
        _run(p, "init", "-q", "-b", "main")
        _write(p, files)
        out["paths"][name] = str(p)
        out["shas"][name] = _commit(p, "initial")
        return p

    def branch(p, name, edits: dict, delete=()):
        _run(p, "checkout", "-q", "-b", name, "main")
        _write(p, edits)
        for d in delete:
            (p / d).unlink()
        _commit(p, name)

    mig = "src/main/resources/db/migration/"
    ledger = repo("ledger-service", {
        _j("ledger/Account.java"): LEDGER_ACCOUNT, _j("ledger/Entry.java"): LEDGER_ENTRY,
        _j("ledger/AccountRepository.java"): LEDGER_ACCOUNT_REPO, _j("ledger/EntryRepository.java"): LEDGER_ENTRY_REPO,
        _j("ledger/AccountService.java"): LEDGER_SERVICE, _j("ledger/AccountController.java"): LEDGER_CONTROLLER,
        "src/test/java/com/acme/ledger/AccountServiceTest.java": LEDGER_TEST,
        mig + "V1__init.sql": V1_LEDGER,
        "tia-services.yaml": "service: ledger-service\nowner: team-ledger\ncriticality: critical\ndatabase: ledger-db\n"})
    repo("billing-service", {
        _j("billing/BillingAccount.java"): BILLING_ACCOUNT, _j("billing/BillingAccountRepository.java"): BILLING_REPO,
        _j("billing/ChargeService.java"): BILLING_SERVICE, _j("billing/BillingController.java"): BILLING_CONTROLLER,
        _j("billing/ChargeRequest.java"): CHARGE_REQUEST,
        "tia-services.yaml": "service: billing-service\nowner: team-billing\ncriticality: critical\ndatabase: ledger-db\n"})
    statements = repo("statements-service", {
        "models.py": STATEMENTS_MODELS, "app.py": STATEMENTS_APP, "db.py": STATEMENTS_DB,
        "alembic/versions/0001_init.py": ALEMBIC_0001,
        "tia-services.yaml": "service: statements-service\nowner: team-statements\ncriticality: critical\ndatabase: ledger-db\n"})
    risk = repo("risk-service", {
        "risk/__init__.py": "", "risk/models.py": RISK_MODELS, "risk/services.py": RISK_SERVICES,
        "risk/migrations/__init__.py": "", "risk/migrations/0001_initial.py": RISK_0001,
        "tia-services.yaml": "service: risk-service\nowner: team-risk\ncriticality: critical\n"})
    audit = repo("audit-service", {
        _j("audit/AuditEvent.java"): AUDIT_EVENT, _j("audit/AuditRepository.java"): AUDIT_REPO,
        _j("audit/AuditService.java"): AUDIT_SERVICE,
        "src/main/resources/db/changelog/db.changelog-master.yaml": LIQUIBASE_MASTER,
        "src/main/resources/db/changelog/changes/001-init.xml": LIQUIBASE_001,
        "tia-services.yaml": "service: audit-service\nowner: team-audit\ncriticality: critical\n"})
    repo("portal-bff", {
        "portal.py": PORTAL,
        "tia-services.yaml": "service: portal-bff\nowner: team-portal\ncriticality: critical\n"
                             "config_keys:\n  STATEMENTS_URL: statements-service\n  BILLING_URL: billing-service\n"})

    v2 = mig + "V2__change.sql"
    branch(ledger, "drop_status", {v2: "ALTER TABLE accounts DROP COLUMN status;\n"})
    branch(ledger, "drop_status_fixed", {v2: "ALTER TABLE accounts DROP COLUMN status;\n",
                                         _j("ledger/Account.java"): LEDGER_ACCOUNT.replace("    private String status;\n\n", "")})
    branch(ledger, "rename_table", {v2: "ALTER TABLE accounts RENAME TO customer_accounts;\n"})
    branch(ledger, "drop_legacy", {v2: "ALTER TABLE accounts DROP COLUMN legacy_flag;\n"})
    branch(ledger, "add_required", {v2: "ALTER TABLE accounts ADD COLUMN branch VARCHAR(10) NOT NULL;\n"})
    branch(ledger, "add_optional", {v2: "ALTER TABLE accounts ADD COLUMN note TEXT;\nALTER TABLE accounts ADD COLUMN tier VARCHAR(5) NOT NULL DEFAULT 'STD';\n"})
    branch(ledger, "widen", {v2: "ALTER TABLE accounts ALTER COLUMN owner_name TYPE VARCHAR(200);\n"})
    branch(ledger, "narrow", {v2: "ALTER TABLE accounts ALTER COLUMN owner_name TYPE VARCHAR(20);\n"})
    branch(ledger, "unparsed", {v2: "CREATE PROCEDURE archive_accounts AS BEGIN DELETE FROM accounts; END;\n"
                                    "ALTER TABLE accounts DROP COLUMN status;\n"})
    branch(ledger, "data_only", {v2: "UPDATE accounts SET status = 'OPEN' WHERE status IS NULL;\n"})
    _run(ledger, "checkout", "-q", "main")
    branch(statements, "alembic_drop", {"alembic/versions/0002_drop_rendered.py": ALEMBIC_0002})
    _run(statements, "checkout", "-q", "main")
    branch(risk, "drop_band", {"risk/migrations/0002_remove_score_band.py": RISK_0002})
    _run(risk, "checkout", "-q", "main")
    branch(audit, "drop_actor", {"src/main/resources/db/changelog/changes/002-drop-actor.yaml": LIQUIBASE_002})
    _run(audit, "checkout", "-q", "main")
    return out
