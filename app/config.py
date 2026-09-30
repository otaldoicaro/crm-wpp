import os

from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "localhost")  # ex: agenciajunta-crm.com.br
SESSION_COOKIE_NAME = "crm_session"
# token secreto pra proteger o endpoint de provisionamento inicial (/admin/bootstrap)
ADMIN_SETUP_TOKEN = os.getenv("ADMIN_SETUP_TOKEN", "")

# Meta (WhatsApp Cloud API + Conversions API)
META_APP_ID = os.getenv("META_APP_ID", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v21.0")
META_PIXEL_ID = os.getenv("META_PIXEL_ID", "")
META_CAPI_ACCESS_TOKEN = os.getenv("META_CAPI_ACCESS_TOKEN", "")
META_WEBHOOK_VERIFY_TOKEN = os.getenv("META_WEBHOOK_VERIFY_TOKEN", "dev-verify-token")
# token separado, com escopo ads_read, usado só pra buscar nome de campanha/anúncio
META_ADS_ACCESS_TOKEN = os.getenv("META_ADS_ACCESS_TOKEN", "")

# Google Ads
GOOGLE_ADS_DEVELOPER_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
GOOGLE_ADS_CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "")
GOOGLE_ADS_CONVERSION_ACTION_ID = os.getenv("GOOGLE_ADS_CONVERSION_ACTION_ID", "")
GOOGLE_ADS_REFRESH_TOKEN = os.getenv("GOOGLE_ADS_REFRESH_TOKEN", "")
GOOGLE_ADS_CLIENT_ID = os.getenv("GOOGLE_ADS_CLIENT_ID", "")
GOOGLE_ADS_CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "")

# Evolution API (WhatsApp não-oficial, self-hosted na VPS)
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "").rstrip("/")  # ex: http://179.236.226.37:8080
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")  # apikey global do Evolution
# segredo que vai na URL do webhook (?token=...) pra só o nosso Evolution conseguir postar mensagens
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "")
# endereço público deste CRM, usado pra montar a URL do webhook ao criar instâncias
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Cópia própria das mídias das conversas (S3/MinIO/R2) — ver app/services/media_store.py
MEDIA_S3_ENDPOINT = os.getenv("MEDIA_S3_ENDPOINT", "")  # ex: http://179.236.226.37:8333 (SeaweedFS no VPS)
MEDIA_S3_ACCESS_KEY = os.getenv("MEDIA_S3_ACCESS_KEY", "")
MEDIA_S3_SECRET_KEY = os.getenv("MEDIA_S3_SECRET_KEY", "")
MEDIA_S3_BUCKET = os.getenv("MEDIA_S3_BUCKET", "crm-midias")
# por quantos dias guardar foto/áudio/vídeo/documento (texto e dados do lead ficam pra sempre)
MEDIA_RETENTION_DAYS = int(os.getenv("MEDIA_RETENTION_DAYS", "90"))
