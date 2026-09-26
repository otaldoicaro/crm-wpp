import os

from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "localhost")  # ex: agenciajunta-crm.com.br
SESSION_COOKIE_NAME = "crm_session"

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
