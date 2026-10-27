# 🕹️ PAC-NEON // Multi-Agent Arcade Orchestrator (Engenharia Neon)

**Pac-Neon (`v0.4.0`)** é o Cockpit Visual Multi-Agentes da **Engenharia Neon**, desenhado para unificar e controlar em tempo real todas as sessões de IA da máquina do colaborador:
- 🧠 **Google Antigravity IDE (`AGY`)** — descoberta automática de conversas ativas via ConnectRPC (`Cascade` / `language_server_macos_arm`) + transcripts em `~/.gemini/antigravity/brain/*`
- 🤖 **Claude Code (`claude`)** — monitoramento de sessões JSONL em `~/.claude/projects/*`, espelho do terminal ao vivo e hooks automáticos de permissão (`PreToolUse`)
- ✨ **Gemini CLI (`gemini`)** & **OpenAI Codex (`codex`)** — descoberta de processos locais, histórico de sessões e injeção direta no TTY
- ⚡ **Superset CLI (`superset-sh/superset`)** — orquestração nativa de Git Worktrees paralelos
- 🛡️ **Monitor Anti-Alucinação & Saúde de Contexto** — telemetria determinística que mede saturação de contexto, *Loop/Thrashing* de tools, *Espiral de Busca (Overthinking)* e *Stall*, com **Self-Handoff em 1 clique (`♻️ Limpar & Continuar Aqui`)**

---

## 🚀 Instalação Rápida (1 Comando para Qualquer Colaborador da Neon)

Clone o repositório em `~/Projects/pac-neon` e execute o instalador:

```bash
git clone https://github.com/RonivaldoCastro/pac-neon.git ~/Projects/pac-neon
cd ~/Projects/pac-neon
./install.sh
```

O script `./install.sh` cria um ambiente virtual isolado em `~/.pac_neon/venv`, instala o pacote `pac-neon`, configura o **LaunchAgent nativo do macOS**, cria o aplicativo **`~/Applications/Pac-Neon.app`** (acessível via Spotlight `Cmd + Espaço`), registra os hooks do Claude Code e abre o Cockpit em **`http://127.0.0.1:8765`**.

### Instalação alternativa via `pip` / `uv`

```bash
cd ~/Projects/pac-neon
pip install -e ".[dev]"
pac-neon install
pac-neon start
```

---

## 🩺 Diagnóstico Self-Service (`pac-neon doctor`)

Qualquer colaborador pode validar seu ambiente local executando:

```bash
pac-neon doctor
```

O comando verifica automaticamente:
1. **Servidor Pac-Neon (`127.0.0.1:8765`)** e LaunchAgent de inicialização automática
2. **Motores de IA instalados** (Antigravity IDE, Claude Code, Gemini CLI, Codex CLI)
3. **Hooks de permissão do Claude Code** em `~/.claude/settings.json`
4. **GitHub CLI (`gh`)** autenticado para resolução automática de PRs da organização `neon`
5. **Toolchain interna (`neon-cli`, `agy`, `glowworm`)**
6. **Permissão de Acessibilidade do macOS (`System Events`)** para injeção direta de teclas em terminais externos (`pac-neon doctor --prompt-accessibility`)
7. **Auto-descoberta dos repositórios locais** do colaborador em `~/Projects`, `~/Git`, `~/Developer`

---

## 🛡️ Monitor Anti-Alucinação & Self-Handoff em 1 Clique

Cada agente exibido no Cockpit possui telemetria real calculada a partir do seu transcript/log local:
- **Saúde de Contexto (`0–100%`)**: Mede o desgaste da janela de contexto combinando bytes de transcript, contagem de passos e erros de ferramentas.
- **Sinais Reais de Risco de Alucinação**:
  - **Loop / Thrashing (`>= 3x`)**: Mesma ferramenta chamada repetidamente no mesmo arquivo/alvo.
  - **Espiral de Busca / Overthinking (`>= 8` leituras seguidas)**: Sequência longa de `view_file` / `grep_search` sem nenhuma edição ou ação conclusiva.
  - **Latência / Stall (`> 120s`)**: Agente em estado ativo sem produzir novos passos.
- **♻️ Limpar & Continuar Aqui (Self-Handoff)**:
  - Extrai automaticamente o objetivo da missão, branch Git atual, `git status --short`, artefatos criados e último progresso.
  - Em agentes de terminal (**Claude Code / Gemini CLI**), envia `/clear` e injeta o pacote de continuidade automaticamente.
  - No **Antigravity IDE**, abre uma conversa limpa já preenchida com o pacote de continuidade e arquiva a sessão saturada.

---

## 🛠️ Comandos do CLI (`pac-neon`)

| Comando | Descrição |
| :--- | :--- |
| `pac-neon start` | Inicia o servidor em background (se parado) e abre `http://127.0.0.1:8765` |
| `pac-neon doctor` | Executa o checklist completo de diagnóstico do ambiente Neon |
| `pac-neon status` | Verifica se o servidor está online na porta `8765` |
| `pac-neon restart` | Reinicia o servidor em background |
| `pac-neon stop` | Encerra o servidor em background |
| `pac-neon uninstall` | Remove o LaunchAgent, `~/Applications/Pac-Neon.app` e binários globais |

---

## 🧪 Executando a Suíte de Testes

```bash
pytest -v
```
