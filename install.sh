#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# 🕹️ PAC-NEON // Instalador 1-Comando para Colaboradores da Neon
# ============================================================================

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAC_HOME="${HOME}/.pac_neon"
VENV_DIR="${PAC_HOME}/venv"

echo ""
echo "🟡 ==================================================================="
echo "🕹️  PAC-NEON // Orquestrador Multi-Agentes da Engenharia Neon (v0.4.0)"
echo "🟡 ==================================================================="
echo "📂 Diretório do projeto: ${REPO_DIR}"

mkdir -p "${PAC_HOME}" "${HOME}/.local/bin"

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "${candidate}")"
    break
  fi
done

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "❌ Python 3.10+ não encontrado. Instale via: brew install python"
  exit 1
fi

echo "🐍 Usando Python: ${PYTHON_BIN}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  echo "📦 Criando ambiente virtual isolado em ${VENV_DIR}..."
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

echo "⚡ Instalando dependências e pacote pac-neon..."
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "${VENV_DIR}/bin/python" -e "${REPO_DIR}" >/dev/null 2>&1 || \
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip && "${VENV_DIR}/bin/pip" install --quiet -e "${REPO_DIR}"
else
  "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
  "${VENV_DIR}/bin/pip" install --quiet -e "${REPO_DIR}"
fi

echo "🔧 Configurando serviço em background, app macOS e hooks de agentes..."
"${VENV_DIR}/bin/python" -m pac_neon.cli install
"${VENV_DIR}/bin/python" -m pac_neon.cli restart --no-open

echo ""
"${VENV_DIR}/bin/python" -m pac_neon.cli doctor || true

echo ""
echo "✅ Instalação concluída com sucesso!"
echo "🌐 Acesse o Cockpit em: http://127.0.0.1:8765"
echo "💡 Comandos disponíveis no seu terminal:"
echo "   • pac-neon start      (Abre o painel e garante o servidor ativo)"
echo "   • pac-neon doctor     (Diagnóstico completo do seu ambiente Neon)"
echo "   • pac-neon status     (Verifica status da porta 8765)"
echo "   • pac-neon restart    (Reinicia o servidor em background)"
echo ""
