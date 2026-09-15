# Bancada de produção — teste das placas CFTV Telemetria V3.2

Não precisa de Claude Code nem de STM32CubeIDE. Precisa de um computador com **Python 3**
(Windows, macOS ou Linux), este repositório (`CFTV_TELEMETRIA_MOPEN-producao`) e a rede da
bancada. O firmware nasce no repositório de desenvolvimento e é publicado aqui em `firmware/`.

## 1. Preparar o computador (uma vez)

1. Instalar o Python 3 (python.org, ou `brew install python` no Mac). No Windows, marcar
   "Add python.exe to PATH" na instalação.
2. Baixar este repositório com `git clone` (é privado: o GitHub pede o login da conta da produção,
   que precisa ser colaboradora com escrita). Não usar o ZIP: o roteiro devolve os relatórios
   por `git push`.
3. Colocar o computador na rede da bancada com IP fixo **192.168.5.x**, máscara 255.255.255.0
   (por exemplo 192.168.5.50). A placa nasce em 192.168.5.101.

## 2. Preparar a bancada

- Fonte 27 V ligada na entrada FONTE da placa (ajustada em 28,0 a 28,8 V).
- Banco de baterias 24 V ligado no conector BATERIA, **antes** de energizar a fonte.
- Rede CA 127/220 V no conector X1 (entrada do DPS): sem ela o sensor de fase e o DPS ficam
  "não avaliados" e o firmware não mede a tensão da rede.
- Carga de 2 A na saída LPR 24 Vca (resistor de 12 Ω / 50 W, ou a carga eletrônica com ponte).
  Sem ela, rodar com `--sem-lpr` (fica anotado na planilha).
- Cabo de rede da placa no mesmo switch do computador. O LED de link da porta tem de acender.

## 3. Rodar o teste

Na pasta do repositório, no terminal (Prompt de Comando no Windows):

```
python3 bancada_placa.py --etiqueta 2026-02-7450
```

(No Windows pode ser `python` em vez de `python3`.)

O roteiro, sozinho:
1. espera a placa responder em 192.168.5.101 (até 2 min) e identifica MAC e serial;
2. atualiza o firmware para o pacote em `firmware/` (o mais novo), se a placa estiver em outra
   versão. Placa **sem bootloader** é recusada: precisa da gravação inicial por SWD;
3. restaura a configuração de fábrica (senha `Neonex@123`, alertas) e acerta o relógio;
4. testa hardware, saídas, LPR, relé, SNMP e as saídas do microcontrolador (P-FET de carga,
   relé do banco, corte da fonte). Cada item sai como `[OK]` ou `[FALHA]` com o valor medido;
5. imprime `RESULTADO: APROVADA` ou `REPROVADA em N item(ns)` com a lista, grava o relatório em
   `relatorios/<etiqueta>_<serial>_<data>.log`, atualiza a linha da placa (chave: MAC) em
   `placas.csv` (abre no Excel; separador `;`) e faz `git pull`, `commit` e `push` sozinho, para
   o resultado aparecer no GitHub. Se o push falhar (sem rede), o commit fica local: rodar
   `git push` depois.

Opções úteis:

| Opção | Uso |
|---|---|
| `--etiqueta X` | etiqueta de rastreamento (vai na planilha). Placa já registrada pode rodar sem: o MAC recupera a etiqueta. |
| `--sem-lpr` | sem carga de 2 A na LPR |
| `--so-lpr` | repetir só a LPR (depois de ligar a carga) |
| `--so-mcu` | repetir só as saídas do MCU |
| `--sem-ota --sem-reset` | repetir testes numa placa já atualizada e configurada |
| `--obs "texto"` | anotação na planilha (retrabalho feito, etc.) |
| `--corte` | teste manual: pede para desligar o disjuntor, a placa tem de seguir 20 s na bateria |
| `--desligar` | no fim, pede para desligar o disjuntor e apaga a placa pelo relé, para embalar |
| `--so-desligar` | só o desligar no banco (placa já testada, hora de embalar) |

## 4. Interpretar as falhas mais comuns

| Item reprovado | Causa já vista no lote |
|---|---|
| `flash W25Q64` / JEDEC ffffff | W25Q64 sem resposta (SPI3): sem OTA |
| `VBAT ADC x INA226 dentro de 0,5 V` com ADC ~36 V | R134 (10 kΩ) do divisor aberto |
| `P-FET desliga e a leitura de corrente responde` | R128/R129 do INA226 abertos (leem kΩ em vez de 10 Ω) |
| `placa segue viva no banco durante o corte` reiniciou | K1 não fecha: BC817 T6, D17, bobina, F1, cabo do banco |
| `bullet N desliga` lento (> 2,5 s) | LED da saída danificado (é a sangria do capacitor) |
| `DPS: monitor do TMOV com sinal` | VR1 ausente / fusível térmico aberto, ou cadeia do OK5 |
| `sensor de fase com sinal` | cadeia do OK1 (R135–R138, CR1, OK1, R139/R140, C101/C102) |
| `LPR correntes das pernas` iA = iB = 0 | sem carga na LPR (ligar e repetir com `--so-lpr`) |

## 5. Embalar

**Nunca desconectar o banco com a placa ligada**: o GND flutua e queima R128/R129 e o BC817.
Ordem: desligar o disjuntor da rede → aba **Reiniciar → Desligar no banco** (ou o roteiro com
`--desligar`) → LED de vida apaga → desconectar o banco.

## 6. Placa que não responde na rede

- LED de vida piscando e link da porta aceso, mas sem ping: alguém mudou o IP. Botão DEFAULT por
  3 s (buzzer bipa, bip longo, reinicia) devolve 192.168.5.101 e a senha de fábrica.
- Sem LED de vida: alimentação, fusíveis, ou gravação inicial por SWD
  (`Ferramentas/gravar.sh`, precisa do STM32CubeIDE e da sonda ST-Link — feito no desenvolvimento).

## 7. Nova versão de firmware ou do roteiro

Quem desenvolve roda `Ferramentas/publica_producao.sh` no repositório do firmware: ele copia o
roteiro, este README, os atalhos e o `.nxfw` novo para cá e faz o push. Na bancada, `git pull`
antes de começar o dia (o próprio roteiro também faz `pull` ao final de cada placa).
