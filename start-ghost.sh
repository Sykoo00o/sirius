#!/bin/bash
cd "$(dirname "$0")"
[ -f .env ] && export $(cat .env | grep -v "^#" | xargs)
export ANTHROPIC_BASE_URL="https://ghost.thesapientcompany.com"
export ANTHROPIC_AUTH_TOKEN="ak_RvdBR4T1tIhyG4rUwF7baYKdBVMUwB0BRezBMbcx"
export ANTHROPIC_MODEL="ghost-x2"
export ANTHROPIC_SMALL_FAST_MODEL="ghost-x2"
export ANTHROPIC_DEFAULT_OPUS_MODEL="ghost-x2"
export ANTHROPIC_DEFAULT_SONNET_MODEL="ghost-x2"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="ghost-x2"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="222822"
claude --strict-mcp-config --mcp-config '{"mcpServers":{}}'
