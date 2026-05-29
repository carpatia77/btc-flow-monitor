#!/bin/bash
# Instala o script de verificação na crontab do sistema (Linux/VPS)
# Horário: 06:00 da manhã (configurado para usar o timezone America/Sao_Paulo)
# E-mail de notificação em caso de erro: deggerbr@gmail.com

SCRIPT_DIR=$(pwd)
PYTHON_PATH=$(which python3)
CRON_FILE="/tmp/btc_monitor_cron"

echo "Configurando cronjob para envio de alertas para deggerbr@gmail.com..."

# Define a variável MAILTO para o envio do log em caso de falha e a timezone
echo "MAILTO=\"deggerbr@gmail.com\"" > $CRON_FILE
echo "CRON_TZ=America/Sao_Paulo" >> $CRON_FILE

# O script roda as 06:00 todos os dias. 
# O "|| echo" garante que se o script falhar (exit 1), o output seja impresso 
# e consequentemente enviado por email pelo deamon do cron. Se retornar 0,
# o output padrão do cron (que não for erro) pode ser silenciado para não enviar email todo dia.
echo "0 6 * * * cd $SCRIPT_DIR && $PYTHON_PATH scripts/deribit_health_check.py > /dev/null" >> $CRON_FILE

crontab $CRON_FILE
rm $CRON_FILE

echo "Crontab instalado com sucesso! A verificação de saúde rodará diariamente às 06:00 (Horário de Brasília)."
echo "Para verificar a configuração atual, digite: crontab -l"
