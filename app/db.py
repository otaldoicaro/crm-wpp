import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./crm.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

logger = logging.getLogger("db")


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def sync_schema() -> None:
    """Migração "pobre" que roda a cada start: além de `create_all` (que só
    cria tabelas que não existem), adiciona colunas novas em tabelas que já
    existem — sem isso, todo campo novo em models.py quebra em produção com
    "coluna não existe" assim que o Postgres já tem a tabela criada de uma
    versão anterior. Não faz DROP/ALTER de tipo/renomeação — só ADD COLUMN.
    Serve enquanto o projeto está nessa fase inicial; se o schema começar a
    exigir migrações de verdade (renomear coluna, mudar tipo, backfill),
    trocar isso por Alembic."""
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                ddl_type = column.type.compile(dialect=engine.dialect)
                default_clause = ""
                if not column.nullable:
                    scalar_default = getattr(column.default, "arg", None)
                    if isinstance(scalar_default, (str, int, float, bool)):
                        default_clause = f" NOT NULL DEFAULT {column.type.literal_processor(dialect=engine.dialect)(scalar_default)}"
                    else:
                        default_clause = " NOT NULL DEFAULT ''"
                logger.warning("sync_schema: adicionando coluna %s.%s (%s)", table.name, column.name, ddl_type)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}{default_clause}'))
