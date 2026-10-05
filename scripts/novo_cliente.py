"""Cria (ou atualiza) um cliente no CRM em produção, via /admin/bootstrap-tenant.

Rodar no Terminal do Mac, dentro da pasta crm:
    python3 scripts/novo_cliente.py

Pergunta tudo na tela; senha e token são digitados escondidos (não aparecem
na tela nem ficam salvos em lugar nenhum). Só usa bibliotecas padrão do Python.
"""

import getpass
import json
import urllib.error
import urllib.request

CRM_URL = "https://crm.novaviseu.com.br"  # qualquer endereço do nosso VPS serve (o cliente vem do "identificador")
THEMES = ["junta", "novaviseu"]


def ask(label: str, default: str = "") -> str:
    value = input(f"{label}{f' [{default}]' if default else ''}: ").strip()
    return value or default


def main() -> None:
    print("\n== Novo cliente no CRM ==\n")
    name = ask("Nome do cliente", "Nova Viseu Autopeças")
    subdomain = ask("Identificador curto (sem espaço/acento)", "novaviseu")
    theme = ask(f"Tema visual {THEMES}", "novaviseu")
    email = ask("E-mail do gestor (login de admin)")
    password = getpass.getpass("Senha pra esse gestor (não aparece ao digitar): ")
    token = getpass.getpass("ADMIN_SETUP_TOKEN (no VPS: grep ADMIN_SETUP_TOKEN /opt/crm-config/crm.env; não aparece ao colar): ")

    payload = json.dumps(
        {"subdomain": subdomain, "tenant_name": name, "theme": theme, "admin_email": email, "admin_password": password}
    ).encode()
    request = urllib.request.Request(
        f"{CRM_URL}/admin/bootstrap-tenant",
        data=payload,
        headers={"Content-Type": "application/json", "X-Setup-Token": token},
        method="POST",
    )
    print("\nEnviando (se o servidor estiver dormindo, pode levar ~1 minuto)...")
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("❌ Token errado (ou ADMIN_SETUP_TOKEN não configurado no Render).")
        else:
            print(f"❌ Erro {exc.code}: {exc.read().decode()[:300]}")
        return

    print("\n✅ Pronto!")
    print(f"   Cliente: {name} ({'criado agora' if data.get('created_tenant') else 'já existia, atualizado'})")
    print(f"   Tema: {data.get('theme')}")
    print(f"   Login do gestor: https://{data.get('subdomain')}.<IP-com-traços>.sslip.io/login (ou o domínio próprio do cliente)")
    print("   Depois de entrar: menu Equipe > copie o link de convite e mande pros vendedores.\n")


if __name__ == "__main__":
    main()
