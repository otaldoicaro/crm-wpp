# Regras do CRM pra qualquer conversa do Claude neste código

Este código é **um sistema só para vários clientes** da Agência Junta, todos no mesmo servidor.
Mudar código afeta clientes que estão em uso. As regras abaixo valem em toda conversa.

## 1. Escopo: o padrão é SÓ o cliente desta conversa
- Toda conversa trabalha **um cliente**. Se ele não estiver claro, pergunte qual é antes de mexer em qualquer coisa.
- **Sem a palavra TODOS, a mudança vale só pra esse cliente.**
  - Coloque no tema, no ramo ou num recurso liga/desliga dele (`app/themes.py`, `app/segments.py`, `app/features.py`, regras CSS em `[data-tema="<tema>"]`).
  - Nunca mude o comportamento dos outros clientes.
- **Mudança que vale pra todos os clientes** só acontece se a pessoa escrever **TODOS** no pedido. Se o pedido parecer "pra todos" mas não disser TODOS, pergunte antes.
- **Mudança pra um ramo inteiro** (ex.: todos os de seminovos) só acontece se a pessoa escrever **RAMO <nome>**.
- **Nunca escreva o nome ou o identificador de um cliente direto na lógica do código.** O que é individual vira configuração.
- Não aproveite a conversa pra "arrumar" outra coisa. Sugira como pedido separado.
- Toda mensagem de commit começa com a etiqueta: `[Só <Cliente>]`, `[Ramo <ramo>]` ou `[Todos]`.
- **Exceção: o Icaro (dono da agência).** No login dele, o `CLAUDE.md` pessoal (`~/.claude/CLAUDE.md`) diz que o
  padrão é TODOS. Nas conversas dele vale essa regra; nas do time, vale esta seção.

## 2. Nada de senha no chat
- Nunca peça nem aceite senha, token ou chave no chat. Esses valores são digitados no terminal, em campo escondido.
- Se alguém colar um no chat, avise e peça pra trocar o valor.
- O repositório é **público**: nenhum segredo entra no código.

## 3. Uma publicação por vez
- Trabalhe num branch do cliente, criado a partir do `main` atualizado:
  ```bash
  git fetch origin && git checkout -b cliente/<identificador> origin/main
  ```
- Antes de publicar:
  1. Confirme com a pessoa que avisou no grupo e que ninguém mais está publicando.
  2. Rode `git fetch origin && git rebase origin/main`.
  3. Teste de novo.
- **Nunca use `--force`.** Se o push for recusado, alguém publicou no meio: faça o rebase e teste de novo.

## 4. Nada pesado no horário comercial (8h às 18h)
- Atualizar o servidor (`instalar.sh`) deixa todos os clientes fora do ar por 1 a 2 minutos.
- O mesmo vale pra scripts com `--aplicar`, recuperações de muitos dias e importações.
- Oriente a pessoa a rodar essas coisas **antes das 8h ou depois das 18h** e avisar no grupo. Durante o dia, só o que for urgente.

## 5. Links do cliente
- Todo link que o cliente vê (convite, rodízio `/go`, formulário) usa o domínio dele: `app.tenancy.public_url(tenant)`.

## 6. Como falar com a equipe
- A equipe não é técnica.
- Fale em português, simples, com o caminho exato de cliques ou o comando pronto pra colar.
- O passo a passo de cliente novo está no guia que a pessoa cola no começo da conversa (`GUIA_EQUIPE_NOVO_CLIENTE.md`).
