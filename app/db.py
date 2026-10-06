import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./crm.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
# o padrão (5 + 10 conexões) esgotava com ~20 pessoas no Inbox atualizando a cada 3s:
# as requisições ficavam na fila esperando conexão e o CRM inteiro parava
pool_args = {} if DATABASE_URL.startswith("sqlite") else {
    "pool_size": 20, "max_overflow": 20, "pool_timeout": 10, "pool_pre_ping": True, "pool_recycle": 1800,
}
engine = create_engine(DATABASE_URL, connect_args=connect_args, **pool_args)
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


def sync_indexes() -> None:
    """Cria os índices declarados nos modelos que ainda não existem no banco.
    O sync_schema só adiciona COLUNAS; sem isso, colunas novas (e algumas antigas,
    como messages.wa_message_id) ficavam sem índice e cada busca lia a tabela inteira."""
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {ix["name"] for ix in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in existing:
                logger.warning("sync_indexes: criando índice %s", index.name)
                index.create(bind=engine, checkfirst=True)
