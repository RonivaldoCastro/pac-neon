---
name: pac-neon
description: Instala, inicia, diagnostica e opera o Pac-Neon — o Cockpit Visual Multi-Agentes da Neon para Antigravity, Claude Code, Gemini CLI, Codex e Superset. Use quando o usuário pedir para abrir o Pac-Neon, instalar o orquestrador de agentes, verificar saúde de contexto/risco de alucinação das conversas, fazer Self-Handoff ou diagnosticar o ambiente com pac-neon doctor.
---

# 🕹️ Pac-Neon — Cockpit Multi-Agentes da Engenharia Neon

O **Pac-Neon** é o orquestrador visual local (`http://127.0.0.1:8765`) que unifica todas as sessões de IA da sua máquina (**Google Antigravity IDE**, **Claude Code**, **Gemini CLI**, **OpenAI Codex** e **Superset Worktrees**) em um único cockpit em tempo real com:
- **Aprovação remota de permissões (`Power Pellets`)** direto pelo navegador sem caçar qual janela de terminal está bloqueada.
- **Monitor Anti-Alucinação & Saúde de Contexto** baseado em telemetria real dos transcripts (`Loop/Thrashing`, `Search Spiral / Overthinking` e `Stall/Latência`).
- **Self-Handoff (`♻️ Limpar & Continuar Aqui`)** em 1 clique para zerar o contexto saturado mantendo 100% do progresso, arquivos alterados e artefatos.
- **Integração nativa com `neon-cli` e `gh`** para vincular automaticamente PRs da organização `neon` e cards do Jira a cada agente.

---

## Como Instalar ou Atualizar (1 Comando)

Se o repositório já estiver clonado em `~/Projects/pac-neon`:

```bash
cd ~/Projects/pac-neon && ./install.sh
```

Ou via `uv` / `pipx`:

```bash
pip install -e ~/Projects/pac-neon
pac-neon install
pac-neon start
```

---

## Comandos Principais do CLI (`pac-neon`)

- **`pac-neon start`**: Inicia o servidor em background (se não estiver rodando) e abre o Cockpit em `http://127.0.0.1:8765`.
- **`pac-neon doctor`**: Executa o autodiagnóstico completo do ambiente do colaborador (Daemon, motores de IA detectados, Hooks do Claude Code, autenticação `gh`, `neon-cli` e permissão de Acessibilidade do macOS).
- **`pac-neon status`**: Exibe se o servidor está ativo na porta `8765`.
- **`pac-neon restart`**: Reinicia o servidor para aplicar atualizações imediatamente.
- **`pac-neon stop`**: Encerra o servidor local.
- **`pac-neon uninstall`**: Remove o LaunchAgent, o atalho `Pac-Neon.app` e os wrappers globais.

---

## Troubleshooting Rápido

1. **Verificar saúde completa do setup**:
   ```bash
   pac-neon doctor
   ```
2. **Injeção de teclas no iTerm / Terminal.app não envia `Enter`**:
   - Rode `pac-neon doctor --prompt-accessibility` e habilite seu terminal/Python em **Ajustes do Sistema > Privacidade e Segurança > Acessibilidade**.
3. **Links de PR do GitHub não aparecem automaticamente nos cards**:
   - Verifique `gh auth status` (autentique com `gh auth login` na organização `neon`).
