#!/usr/bin/env python3
"""Roteiro de bancada, placa a placa (2026-09-13).

Uso:  Ferramentas/bancada_placa.py [--etiqueta 2026-02-7436] [--ip 192.168.5.101]
                                   [--fw Debug/neonex_cftv_v-X.nxfw] [--sem-reset] [--sem-ota] [--botao]

Sequencia:
  1. espera a placa responder e faz login (senha nova ou antiga);
  2. identifica (MAC, serial, firmware, uptime);
  3. atualiza por OTA para o pacote escolhido (o mais novo de Debug/ sem -dev);
     placa sem bootloader e' recusada com a instrucao de gravar por SWD;
  4. restaura a configuracao de fabrica (senha, alertas, limites) e acerta o relogio;
  5. testa: I2C (INA3221 x2, INA226, SHT30), fonte, bateria (ADC x INA226),
     sensores, RTC, saidas 12 V (liga/desliga com leitura de tensao), saida
     LPR 24 Vca (ponte sem falha, RMS regulado, correntes das pernas se
     houver carga = retrabalho SEL0), rele auxiliar, SNMP, contagem de coulomb;
  6. saidas do MCU pelo efeito no INA226 (fw >= .144, POST /api/hw/pino): P-FET liga/desliga
     a corrente de carga, K1 aberto zera a corrente, DSB_FONTE poe a placa no banco por 2,5 s
     sem reiniciar; buzzer e LED forcados para conferencia visual. --so-mcu faz so isso.
  7. com --corte, pede para desligar a rede CA: a placa tem de seguir viva em bateria
     20 s (banco fornecendo, rele fechado, saidas ligadas) e nao reiniciar; --so-corte
     faz so isso. Com --botao, pede para segurar o DEFAULT 3 s e confere o reinicio.
Sem dependencias alem do Python 3 (OTA e SNMP feitos aqui mesmo). No repo de producao
(CFTV_TELEMETRIA_MOPEN-producao) o relatorio vai para relatorios/ e a linha da placa para
placas.csv, e o script faz git pull/commit/push sozinho; no repo do firmware, Debug/bancada/
e Docs/bancada/placas.csv.
"""
import argparse, glob, json, os, re, subprocess, sys, time, urllib.request, urllib.error

AQUI = os.path.dirname(os.path.abspath(__file__))
# Dois layouts, mesmo script:
#  - desenvolvimento: este arquivo em Ferramentas/ do repo do firmware; pacote em Debug/,
#    planilha em Docs/bancada/placas.csv, relatorios em Debug/bancada/ (fora do git);
#  - producao: este arquivo na raiz do repo CFTV_TELEMETRIA_MOPEN-producao, com firmware/,
#    placas.csv e relatorios/ versionados; ao final de cada placa faz git pull/commit/push.
PRODUCAO = os.path.isdir(os.path.join(AQUI, "firmware"))
PROJ = AQUI if PRODUCAO else os.path.dirname(AQUI)
FW_DIR = os.path.join(PROJ, "firmware") if PRODUCAO else os.path.join(PROJ, "Debug")
REL_DIR = os.path.join(PROJ, "relatorios") if PRODUCAO else os.path.join(PROJ, "Debug", "bancada")
SENHAS = ["Neonex@123", "neonex"]
COMMUNITY = "neonex-facial"

class Placa:
    def __init__(self, ip):
        self.ip = ip; self.senha = None; self.tok = None
    def _req(self, path, body=None, timeout=8):
        url = f"http://{self.ip}{path}{'&' if '?' in path else '?'}t={self.tok or ''}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    def login(self, senhas=SENHAS):
        for s in senhas:
            try:
                req = urllib.request.Request(f"http://{self.ip}/api/login", data=json.dumps({"user": "admin", "password": s}).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=5) as r:
                    d = json.loads(r.read())
                if d.get("token"):
                    self.tok = d["token"]; self.senha = s; return True
            except Exception:
                pass
        return False
    def get(self, path, tries=3):
        for k in range(tries):
            try:
                self.login([self.senha] if self.senha else SENHAS)
                st, body = self._req(path)
                if st == 200 and body.strip():
                    return json.loads(body)
            except Exception:
                time.sleep(1)
        raise RuntimeError(f"GET {path} falhou")
    def post(self, path, body):
        self.login([self.senha] if self.senha else SENHAS)
        try:
            st, txt = self._req(path, body)
        except urllib.error.HTTPError as e:
            st, txt = e.code, e.read().decode(errors="replace")
        try: return st, json.loads(txt)
        except Exception: return st, {"raw": txt}

def ota_python(p, fw, rel):
    """Envia o .nxfw, aplica e espera a placa voltar. Sem depender do ota.sh (zsh)."""
    dados = open(fw, "rb").read()
    for tentativa in (1, 2):
        try:
            p.login([p.senha] if p.senha else SENHAS)
            req = urllib.request.Request(f"http://{p.ip}/api/firmware?t={p.tok}", data=dados, method="POST",
                                         headers={"Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read())
            if resp.get("estado") == "preparada": break
            rel.log(f"  envio: estado '{resp.get('estado')}', erro '{resp.get('erro')}'")
        except Exception as e:
            rel.log(f"  envio sem resposta (tentativa {tentativa}): {e}")
            resp = {}
        time.sleep(5)
    if resp.get("estado") != "preparada":
        return None
    try:
        p.post("/api/firmware/aplicar", {})
    except Exception:
        pass
    rel.log("  aplicando, aguardando reinicio...")
    time.sleep(15)
    p.tok = None; p.senha = None
    for _ in range(45):
        try:
            if p.login():
                v = p.get("/api/firmware", tries=1).get("versaoAtual")
                if v: return v
        except Exception:
            pass
        time.sleep(2)
    return None

def snmp_get(ip, community, oids, timeout=3):
    """SNMP v2c GET minimo (BER) em UDP: devolve {oid: valor} ou {} sem resposta."""
    import socket, struct
    def tlv(tag, body): 
        n = len(body)
        ln = bytes([n]) if n < 128 else (bytes([0x80 | 2]) + struct.pack(">H", n))
        return bytes([tag]) + ln + body
    def enc_oid(o):
        parts = [int(x) for x in o.strip(".").split(".")]
        out = bytes([40 * parts[0] + parts[1]])
        for v in parts[2:]:
            b = [v & 0x7F]; v >>= 7
            while v: b.insert(0, 0x80 | (v & 0x7F)); v >>= 7
            out += bytes(b)
        return tlv(0x06, out)
    def enc_int(v): return tlv(0x02, v.to_bytes(4, "big", signed=True).lstrip(b"\x00") or b"\x00")
    vbl = tlv(0x30, b"".join(tlv(0x30, enc_oid(o) + tlv(0x05, b"")) for o in oids))
    pdu = tlv(0xA0, enc_int(0x1234) + enc_int(0) + enc_int(0) + vbl)
    msg = tlv(0x30, enc_int(1) + tlv(0x04, community.encode()) + pdu)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(timeout)
    try:
        s.sendto(msg, (ip, 161)); data, _ = s.recvfrom(1500)
    except Exception:
        return {}
    finally:
        s.close()
    # decodificacao simples: procura pares OID/INTEGER na resposta
    res = {}; i = 0
    def rd(i):
        tag = data[i]; ln = data[i + 1]; j = i + 2
        if ln & 0x80: k = ln & 0x7F; ln = int.from_bytes(data[j:j + k], "big"); j += k
        return tag, data[j:j + ln], j + ln
    def walk(i, end):
        while i < end:
            tag, body, nxt = rd(i)
            if tag == 0x30 and len(body) > 2 and body[0] == 0x06:
                t2, ob, j2 = rd(i + (nxt - i - len(body)))
                oid_b = ob; k = 0; parts = [oid_b[0] // 40, oid_b[0] % 40]; v = 0
                for b in oid_b[1:]:
                    v = (v << 7) | (b & 0x7F)
                    if not b & 0x80: parts.append(v); v = 0
                oid = "1.3." + ".".join(str(x) for x in parts[2:]) if parts[:2] == [1, 3] else ".".join(map(str, parts))
                t3, vb, _ = rd(j2)
                if t3 == 0x02: res[oid] = int.from_bytes(vb, "big", signed=True)
                else: res[oid] = vb
            elif tag in (0x30, 0xA2):
                walk(i + (nxt - i - len(body)), nxt)
            i = nxt
    walk(0, len(data))
    return res

class Relatorio:
    def __init__(self): self.itens = []; self.linhas = []
    def log(self, s):
        print(s); self.linhas.append(s)
    def item(self, nome, ok, detalhe=""):
        tag = "OK   " if ok else "FALHA"
        self.log(f"  [{tag}] {nome}" + (f": {detalhe}" if detalhe else ""))
        self.itens.append((nome, bool(ok), detalhe))
    def falhas(self): return [i for i in self.itens if not i[1]]

def espera_placa(ip, rel, seg=120):
    rel.log(f"== esperando a placa em {ip} (ate {seg} s) ==")
    p = Placa(ip); ini = time.time()
    while time.time() - ini < seg:
        if p.login():
            rel.log(f"  respondeu; senha aceita: {'nova (Neonex@123)' if p.senha == 'Neonex@123' else 'ANTIGA (neonex)'}")
            return p
        time.sleep(2)
    return None

def set_saida(p, sid, ligada):
    st = p.get("/api/status")
    cur = st["saidas"].get(sid, {}).get("ligada")
    if cur is None: return None
    if bool(cur) != bool(ligada):
        p.post("/api/cmd", {"id": sid, "acao": "toggle"})
        time.sleep(2.5)
    return p.get("/api/status")["saidas"][sid]

def forca(p, pino, nivel, ms):
    st, r = p.post("/api/hw/pino", {"pino": pino, "nivel": nivel, "ms": ms})
    return st == 200, r.get("error", r.get("status", ""))

def ibat(p):
    return p.get("/api/status")["bateria"]["i"]

def testa_saidas_mcu(p, rel):
    """Saidas do MCU pelo efeito no INA226 (POST /api/hw/pino, fw >= 3.2.0.144):
       P-FET on -> corrente de carga aparece; P-FET off -> some (tambem prova que a
       leitura de corrente responde); K1 aberto com carga -> corrente some;
       DSB_FONTE -> placa vai para o banco (corrente negativa) e segue viva."""
    rel.log("== saidas do MCU (forcando pinos) ==")
    st = p.get("/api/status")
    if st["fonte"]["v"] < 25.5:
        rel.log("  [--   ] saidas do MCU nao avaliadas: fonte ausente (precisa de rede para forcar com seguranca)"); return
    if st["bateria"]["v"] < 20:
        rel.log("  [--   ] saidas do MCU nao avaliadas: banco ausente"); return
    ok, msg = forca(p, "PFET_CARGA", 1, 9000)
    if not ok:
        rel.item("comando de forcar pino disponivel", False, f"{msg} (firmware antigo?)"); return
    time.sleep(2.5); i_on = ibat(p)
    rel.item("OUT_ENB_PFET_CARGA liga (corrente de carga > 0,1 A com o P-FET forcado)", i_on > 0.1, f"{i_on} A")
    forca(p, "PFET_CARGA", 0, 9000); time.sleep(3); i_off = ibat(p)
    ina_ok = -0.3 < i_off < 0.1
    rel.item("OUT_ENB_PFET_CARGA desliga e a leitura de corrente responde (< 0,1 A)", ina_ok,
             f"{i_off} A" + ("" if ina_ok else ": leitura presa (INA226/shunt) ou caminho paralelo de carga (Q4/fiacao)"))
    # K1: com o P-FET ligado a corrente passa pelo K1; abrir o K1 tem de zerar
    if not ina_ok:
        rel.log("  [--   ] K1 pela corrente nao avaliavel: leitura do INA226 presa")
    else:
        forca(p, "PFET_CARGA", 1, 9000); time.sleep(2.5); i1 = ibat(p)
        if i1 > 0.1:
            forca(p, "RELE_BAT", 0, 4000); time.sleep(2); i2 = ibat(p)
            rel.item("OUT_ENB_RELE_BAT abre o K1 (corrente de carga some com o rele aberto)", abs(i2) < 0.1,
                     f"{i1} A -> {i2} A" + ("" if abs(i2) < 0.1 else ": K1 nao abriu (contato colado, START_MANUAL, BC817) ou caminho fora do K1"))
            time.sleep(3)
        else:
            rel.log("  [--   ] K1 nao avaliado: sem corrente de carga para interromper")
    time.sleep(7)
    # DSB_FONTE: VFONTE e' medido ANTES do diodo ideal e nao cai; quem cai e' o barramento,
    # visivel no pico da saida LPR (burst voutMax ~ barramento). A placa tem de seguir viva.
    set_saida(p, "lpr", 1); time.sleep(1)
    vbus0 = p.get("/api/hw").get("burst", {}).get("voutMax", 0)
    up0 = p.get("/api/device")["uptimeSeconds"]
    ok, msg = forca(p, "DSB_FONTE", 1, 2500)
    if not ok:
        rel.item("OUT_DSB_FONTE (corte da fonte por 2,5 s)", False, msg); return
    time.sleep(0.8)
    try:
        hw1 = p.get("/api/hw", tries=1); vbus1 = hw1.get("burst", {}).get("voutMax", 0); viva = True
        ib = p.get("/api/status", tries=1)["bateria"]["i"]
    except Exception:
        vbus1 = None; ib = None; viva = False
    time.sleep(3)
    try:
        up1 = p.get("/api/device")["uptimeSeconds"]; reiniciou = up1 < up0
    except Exception:
        reiniciou = True
    vbat = p.get("/api/status")["bateria"]["v"] * 1000
    caiu = viva and vbus1 is not None and vbus1 < vbus0 - 1500
    rel.item("OUT_DSB_FONTE corta a fonte (barramento cai da fonte para o banco durante o corte)",
             caiu or vbat > vbus0 - 1500,
             f"barramento {vbus0} -> {vbus1} mV (banco {vbat:.0f} mV)" + ("" if caiu else (" banco proximo da fonte: sem margem para ver a queda" if vbat > vbus0 - 1500 else ": fonte nao foi cortada (T11/LM74800 EN)")))
    rel.item("placa segue viva no banco durante o corte (sem reiniciar)", viva and not reiniciou,
             f"reiniciou={reiniciou}" + (f", corrente {ib} A" if ina_ok else ", corrente nao avaliavel (INA226 preso)") +
             ("" if (viva and not reiniciou) else ": K1 nao sustenta o barramento (BC817/bobina/F1/cabo)"))
    forca(p, "BUZZER", 1, 300); forca(p, "LED", 1, 1000)
    rel.log("  [--   ] OUT_BUZZER (bip de 0,3 s) e OUT_LED (aceso 1 s) forcados: conferir a olho/ouvido")

def desliga_no_banco(p, rel):
    rel.log("== desligar no banco ==")
    rel.log(">> DESLIGUE O DISJUNTOR da rede (esperando ate 120 s a fonte cair)...")
    ini = time.time(); caiu = False
    while time.time() - ini < 120:
        try:
            if p.get("/api/status", tries=1)["fonte"]["v"] < 24.0: caiu = True; break
        except Exception: pass
        time.sleep(1)
    if not caiu:
        rel.item("desligar no banco", False, "fonte nao caiu em 120 s"); return
    time.sleep(2)
    st, r = p.post("/api/device/desligar", {})
    if st != 200:
        rel.item("desligar no banco", False, r.get("error", str(r))); return
    time.sleep(4); viva = True
    try:
        p.tok = None
        if p.login() and p.get("/api/status", tries=1): viva = True
    except Exception: viva = False
    rel.item("placa apagou pelo K1 (pode desconectar o banco)", not viva, "ainda responde: K1 nao abriu" if viva else "sem resposta, como esperado")

def testa_corte(p, rel, ip):
    """Corte de rede manual: a placa tem de continuar viva, com o banco fornecendo,
    rele da bateria fechado e saidas ligadas. Placa que some ou reinicia = banco nao
    sustenta o barramento (K1/BC817/diodo da bobina, F1, cabo do banco)."""
    rel.log("== teste em bateria ==")
    up0 = p.get("/api/device")["uptimeSeconds"]; t_up0 = time.time()
    rel.log(">> DESLIGUE A REDE CA agora (esperando ate 120 s a fonte cair)...")
    ini = time.time(); caiu = False
    while time.time() - ini < 120:
        try:
            st = p.get("/api/status", tries=1)
            if st["fonte"]["v"] < 24.0: caiu = True; break
        except Exception: caiu = True; break   # ja morreu na transicao
        time.sleep(1)
    if not caiu:
        rel.item("corte de rede executado", False, "fonte nao caiu em 120 s"); return
    ok = True; det = []; mortas = 0; ini = time.time()
    while time.time() - ini < 20:
        try:
            st = p.get("/api/status", tries=1); b = st["bateria"]; h = st["hw"]
            on = sum(1 for k, v in st["saidas"].items() if isinstance(v, dict) and v.get("ligada"))
            det.append(f"{b['v']}V {b['i']}A rele={int(h['releBateria'])} on={on}")
            if b["i"] > -0.05 or not h["releBateria"]: ok = False
            mortas = 0
        except Exception:
            mortas += 1
            if mortas >= 3: ok = False; det.append("placa parou de responder"); break
        time.sleep(2)
    rel.item("placa segue viva em bateria por 20 s (banco fornecendo, rele fechado)", ok, "; ".join(det[-3:]))
    rel.log(">> RELIGUE A REDE CA (esperando ate 120 s)...")
    ini = time.time(); voltou = False
    while time.time() - ini < 120:
        try:
            p.tok = None
            if p.login() and p.get("/api/status", tries=1)["fonte"]["v"] > 25.5: voltou = True; break
        except Exception: pass
        time.sleep(2)
    if not voltou:
        rel.item("rede religada", False, "fonte nao voltou em 120 s"); return
    up1 = p.get("/api/device")["uptimeSeconds"]
    reiniciou = up1 < (time.time() - t_up0) - 5
    rel.item("placa nao reiniciou durante o corte", not reiniciou, f"uptime {up0} -> {up1} s")

def testa_lpr(p, rel):
    rel.log("== saida LPR 24 Vca ==")
    lpr = set_saida(p, "lpr", 1)
    time.sleep(3)
    hw = p.get("/api/hw"); c = hw["ponte"]["canais"][2]; bu = hw.get("burst", {})
    rel.item("LPR ligada sem falha", lpr is not None and lpr["ligada"] and c["falha"] == 0, f"falha={c['falha']} csMv={c['csMv']}")
    rel.item("LPR pico na saida >= 20 V", bu.get("voutMax", 0) >= 20000, f"voutMax {bu.get('voutMax')} mV, pctAlto {bu.get('pctAlto')} %")
    rel.item("LPR sem codigo de falha no CS (< 3,0 V na janela)", c["csMv"] < 3000, f"{c['csMv']} mV")
    on_us = hw["ponte"]["onUs"]
    rel.item("LPR regulacao de RMS ativa (4000-8333 us)", 4000 <= on_us <= 8333, f"onUs {on_us}, RMS calc {c['tensaoMv']} mV")
    ia, ib = c["iA"], c["iB"]
    if ia + ib > 300:
        sim = abs(ia - ib) * 100 // max(ia, ib, 1)
        rel.item("LPR correntes das pernas (retrabalho SEL0->INA)", sim <= 30, f"iA {ia} mA, iB {ib} mA, assimetria {sim} %")
    else:
        rel.item("LPR correntes das pernas (retrabalho SEL0->INA)", False, f"iA {ia} iB {ib}: SEM carga na LPR ou SEM retrabalho SEL0 (ligue a carga de 2 A e repita com --so-lpr)")
    off = set_saida(p, "lpr", 0); time.sleep(1)
    hw2 = p.get("/api/hw")
    rel.item("LPR desliga (pico < 3 V)", hw2.get("burst", {}).get("voutMax", 99999) < 3000, f"voutMax {hw2.get('burst', {}).get('voutMax')} mV")
    set_saida(p, "lpr", 1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.5.101")
    ap.add_argument("--fw", default=None, help="pacote .nxfw (padrao: o mais novo de Debug/ sem -dev)")
    ap.add_argument("--sem-reset", action="store_true", help="nao restaurar a configuracao de fabrica")
    ap.add_argument("--sem-ota", action="store_true")
    ap.add_argument("--botao", action="store_true", help="ao final, testar o botao DEFAULT (3 s)")
    ap.add_argument("--etiqueta", default="", help="etiqueta de rastreamento da placa (vai no relatorio e no nome do arquivo)")
    ap.add_argument("--so-lpr", action="store_true", help="repetir so o bloco da saida LPR (ex.: depois de ligar a carga de 2 A)")
    ap.add_argument("--obs", default="", help="observacao para a linha da placa na planilha (retrabalho feito, etc.)")
    ap.add_argument("--corte", action="store_true", help="ao final, teste manual de corte de rede (a placa tem de seguir em bateria)")
    ap.add_argument("--so-corte", action="store_true", help="so o teste de corte de rede")
    ap.add_argument("--so-mcu", action="store_true", help="so o teste das saidas do MCU (forcar pinos)")
    ap.add_argument("--sem-lpr", action="store_true", help="pular a saida LPR (sem carga de 2 A na bancada); fica anotado na planilha")
    ap.add_argument("--desligar", action="store_true", help="ao final, pede para desligar o disjuntor e apaga a placa pelo K1 (para embalar)")
    ap.add_argument("--so-desligar", action="store_true", help="so desligar no banco: pede o disjuntor e apaga a placa pelo K1")
    a = ap.parse_args()
    if a.so_lpr or a.so_corte or a.so_mcu or a.so_desligar: a.sem_ota = a.sem_reset = True
    rel = Relatorio()

    fw = a.fw
    if not fw and not a.sem_ota:
        # pacote = a versao do ultimo build (Core/Inc/fw_version.h), nunca "o mais
        # novo de Debug/": um .nxfw antigo com numeracao diferente ja foi parar
        # numa placa por ordenacao (2026-09-13).
        fwh = os.path.join(PROJ, "Core", "Inc", "fw_version.h")
        m = re.search(r'FW_VERSION_STR\s+"([^"]+)"', open(fwh).read()) if (not PRODUCAO and os.path.exists(fwh)) else None
        if m and not m.group(1).endswith("-dev") and os.path.exists(os.path.join(FW_DIR, m.group(1) + ".nxfw")):
            fw = os.path.join(FW_DIR, m.group(1) + ".nxfw")                 # maquina de desenvolvimento: o build atual
        else:
            # producao: o pacote liberado em firmware/ (o mais novo pela numeracao)
            cand = [c for c in glob.glob(os.path.join(FW_DIR, "neonex_cftv_v-3.2.0.*.nxfw")) if "-dev" not in c]
            cand.sort(key=lambda f: int(re.search(r"3\.2\.0\.(\d+)", f).group(1)))
            if not cand:
                print(f"nenhum pacote .nxfw em {FW_DIR}: passe --fw"); sys.exit(2)
            fw = cand[-1]
    fw_ver = re.search(r"(neonex_cftv_v-[\d.]*\d)", os.path.basename(fw)).group(1) if fw else None
    rel.log(f"pacote alvo: {fw_ver or 'nenhum'}" + (f"   etiqueta: {a.etiqueta}" if a.etiqueta else ""))

    p = espera_placa(a.ip, rel)
    if not p:
        rel.log("placa nao respondeu: conferir alimentacao, cabo e IP (fabrica 192.168.5.101)"); sys.exit(2)

    # ---- identificacao ------------------------------------------------
    dev = p.get("/api/device")
    # etiqueta gravada na propria placa (campo 'nome'): rodada sem --etiqueta le de la,
    # mesmo com a placa dentro da caixa; rodada com --etiqueta grava (apos o reset de fabrica)
    if not a.etiqueta and re.fullmatch(r"\d{4}-\d{2}-\d{4}", dev.get("nome", "") or ""):
        a.etiqueta = dev["nome"]; rel.log(f"  etiqueta lida da placa: {a.etiqueta}")
    serial = (a.etiqueta.replace("/", "-") + "_" if a.etiqueta else "") + dev.get("serial", "semserial")
    meta = {"etiqueta": a.etiqueta, "mac": dev.get("mac", ""), "serial": dev.get("serial", ""), "firmware": dev.get("firmware", ""), "obs": a.obs}
    rel.log(f"== placa: MAC {dev.get('mac')}  serial {serial}  fw {dev.get('firmware')}  uptime {dev.get('uptimeSeconds')} s ==")

    # ---- firmware -----------------------------------------------------
    if a.so_lpr:
        testa_lpr(p, rel); fim(rel, serial, meta); sys.exit(1 if rel.falhas() else 0)
    if a.so_corte:
        testa_corte(p, rel, a.ip); fim(rel, serial, meta); sys.exit(1 if rel.falhas() else 0)
    if a.so_mcu:
        testa_saidas_mcu(p, rel); fim(rel, serial, meta); sys.exit(1 if rel.falhas() else 0)
    if a.so_desligar:
        desliga_no_banco(p, rel); sys.exit(1 if rel.falhas() else 0)
    fwi = p.get("/api/firmware")
    if not fwi.get("bootloader"):
        rel.item("bootloader presente", False, "placa SEM bootloader: gravar por SWD com Ferramentas/gravar.sh e rodar de novo")
        fim(rel, serial, meta); sys.exit(3)
    rel.item("bootloader presente", True, f"flash externa {'ok' if fwi.get('flashOk') else 'FALHA'}, jedec {fwi.get('jedec')}")
    if fwi.get("flashOk") is False:
        rel.item("flash W25Q64", False, "sem flash externa: OTA impossivel")
    if fw and not a.sem_ota and fwi.get("versaoAtual") != fw_ver:
        rel.log(f"  atualizando {fwi.get('versaoAtual')} -> {fw_ver} ...")
        nova = ota_python(p, fw, rel)
        rel.log(f"  placa voltou em {nova}" if nova else "  placa nao voltou apos o OTA")
        p.tok = None
        p = espera_placa(a.ip, rel, 90) or p
        fwi = p.get("/api/firmware")
        rel.item("OTA para o pacote alvo", fwi.get("versaoAtual") == fw_ver, fwi.get("versaoAtual"))
        meta["firmware"] = fwi.get("versaoAtual", "")
    else:
        rel.item("firmware", not fw or fwi.get("versaoAtual") == fw_ver, fwi.get("versaoAtual"))

    # ---- configuracao de fabrica + relogio ------------------------------
    if not a.sem_reset:
        st, r = p.post("/api/device/reset", {})
        rel.item("configuracao restaurada ao padrao", st == 200, r.get("status", r.get("error", "")))
        p.tok = None; p.senha = None; time.sleep(1)
        ok = p.login(["Neonex@123"])
        rel.item("senha de fabrica Neonex@123 aceita", ok)
        if not ok: p.login()
    if a.etiqueta:
        st, r = p.post("/api/device/nome", {"nome": a.etiqueta})
        rel.item("etiqueta gravada no nome da placa", st == 200, a.etiqueta if st == 200 else r.get("error", ""))
    t = time.localtime()
    st, r = p.post("/api/device/hora", {"ano": t.tm_year, "mes": t.tm_mon, "dia": t.tm_mday, "hora": t.tm_hour, "min": t.tm_min, "seg": t.tm_sec})
    rel.item("relogio acertado", st == 200, r.get("status", ""))
    cfg = p.get("/api/settings")
    al = cfg.get("alarmes", {})
    rel.item("alerta de fonte alta em 29,5 V", al.get("fonteMaxMv") == 29500, f"{al.get('fonteMaxMv')} mV")

    # ---- hardware -------------------------------------------------------
    rel.log("== hardware ==")
    hw = p.get("/api/hw")
    for dv in ("ina3221_40", "ina3221_41", "ina226", "sht30"):
        d = hw.get(dv, {})
        rel.item(f"I2C {dv}", d.get("ok"), f"addr {d.get('addr')} bus {d.get('bus')} falhas {d.get('falhas')}")
    st = p.get("/api/status")
    vf = st["fonte"]["v"]
    rel.item("fonte 26,0-29,5 V com rede presente", st["rede"]["presente"] and 26.0 <= vf <= 29.5, f"{vf} V")
    eng = hw.get("eng", {})
    vb_adc, vb_ina = eng.get("vbatAdcMv", 0), eng.get("vbatMv", 0)
    rel.item("bateria plausivel (> 20 V)", vb_ina > 20000 or vb_adc > 20000, f"INA226 {vb_ina} mV, ADC {vb_adc} mV")
    if vb_ina > 5000 and vb_adc > 5000:
        rel.item("VBAT ADC x INA226 dentro de 0,5 V", abs(vb_adc - vb_ina) <= 500, f"diferenca {vb_adc - vb_ina} mV")
    # a contagem parte ~20 s depois do boot (INA226 precisa converter): espera ate 45 s
    for _ in range(15):
        b = p.get("/api/status")["bateria"]
        if b.get("socModo") == "coulomb": break
        time.sleep(3)
    rel.item("SoC por contagem de coulomb", b.get("socModo") == "coulomb", f"{b.get('soc')} %, {b.get('i')} A, estagio {b.get('estagio')}")
    # fonte acima do banco e bateria descarregando = caminho fonte -> barramento aberto
    # (fusivel, diodo, rele) — placa 7444 no painel, 2026-09-14: 28,5 V na fonte e -0,9 A no banco
    if vf * 1000 > vb_ina + 1000 and vb_ina > 20000:
        rel.item("fonte alimenta o barramento (banco nao descarrega com a fonte acima dele)", b.get("i", 0) > -0.2,
                 f"fonte {vf} V, banco {vb_ina/1000:.2f} V, corrente do banco {b.get('i')} A")
    # corrente "de carga" de amperes com a tensao do banco parada nao e' bateria: K1 aberto
    # (BC817/bobina) ou carga do painel pendurada nos terminais do banco, fora do shunt
    # (placa 7444, 2026-09-14: 1,2 a 3,7 A com o banco cravado em 25,51 V por 8 min)
    if b.get("i", 0) > 1.0:
        v0 = p.get("/api/status")["bateria"]["v"]; imin = b["i"]
        for _ in range(12):
            time.sleep(5); bb = p.get("/api/status")["bateria"]; imin = min(imin, bb["i"])
        v1 = bb["v"]
        if imin > 0.8:
            rel.item("corrente do shunt entra no banco (tensao sobe com > 0,8 A por 60 s)", (v1 - v0) >= 0.02,
                     f"{v0} -> {v1} V com {imin:.2f} A: " + ("ok" if (v1 - v0) >= 0.02 else "banco parado: K1 nao fecha ou carga externa nos terminais do banco"))
    # sensor de fase: se o opto do TMOV (mesmo tipo de circuito, mesma entrada CA) ve a rede,
    # o sensor de fase tem de ver tambem; se nao, R135-R138/CR1/OK1/R139/R140/C101/C102 ou PA5
    tmov = eng.get("tmovMv", 0); fase = eng.get("faseMediaMv", 0)
    if tmov > 1000:
        rel.item("sensor de fase com sinal (rede CA no conector)", fase > 700,
                 f"faseMedia {fase} mV, tmov {tmov} mV, rede {st['rede']['tensao']} V")
    elif fase > 700:
        # rede no conector (sensor de fase ve) mas o monitor do TMOV nao: e' o que o painel
        # mostra como "DPS FIM DE VIDA" — fusivel termico do VR1 aberto, VR1 ausente, ou a
        # cadeia R141-R144/CR4/OK5/R145/R146/C103/C123 (placa 2026-09-14)
        rel.item("DPS: monitor do TMOV com sinal (rede CA presente no conector)", False,
                 f"fase {fase} mV, tmov {tmov} mV: VR1 (TMOV14RP275M) ausente/fusivel termico aberto ou cadeia do OK5")
    else:
        rel.log("  [--   ] sensor de fase e DPS nao avaliados: sem rede CA no conector X1 (fase %d mV, tmov %d mV)" % (fase, tmov))
    sn = st["sensores"]
    rel.item("temperatura da placa 0-70 C", 0 < sn["tempPlaca"] < 70, f"{sn['tempPlaca']} C")
    rel.item("temperatura do painel 0-60 C", 0 < sn["tempPainel"] < 60, f"{sn['tempPainel']} C")
    rel.item("umidade 5-95 %", 5 < sn["umidade"] < 95, f"{sn['umidade']} %")
    dev = p.get("/api/device")
    rel.item("RTC sincronizado", bool(dev.get("horaSincronizada")), dev.get("dataHora"))

    # ---- saidas 12 V ----------------------------------------------------
    rel.log("== saidas 12 Vcc ==")
    for k in range(1, 5):
        sid = f"bullet{k}"
        if sid not in st["saidas"]:
            rel.item(f"{sid} montada", False, "nao aparece no status"); continue
        # desligada le ~2,2 V residuais (divisor/INA3221). Sem carga, quem descarrega
        # o capacitor de saida e' o LED da saida: descarga lenta (> 2,5 s) = LED
        # danificado (2026-09-13, placa 7442, bullet2). Espera ate 12 s e anota.
        off = set_saida(p, sid, 0); t_off = 2.5; v_ini = off and off["v"]
        while off is not None and off["v"] >= 4.0 and t_off < 12.5:
            time.sleep(2); t_off += 2
            off = p.get("/api/status")["saidas"][sid]
        on = set_saida(p, sid, 1)
        okd = off is not None and off["v"] < 4.0
        okl = on is not None and 11.0 <= on["v"] <= 13.0
        rel.item(f"{sid} desliga (VOUT < 4 V em ate 12 s)", okd,
                 f"{off and off['v']} V em {t_off:.0f} s" + (f" (lento: {v_ini} V aos 2,5 s)" if t_off > 2.5 else ""))
        rel.item(f"{sid} liga (11-13 V)", okl, f"{on and on['v']} V, {on and on['i']} A")

    if a.sem_lpr:
        rel.log("  [--   ] saida LPR nao testada (--sem-lpr)")
        meta["obs"] = (meta.get("obs", "") + "; " if meta.get("obs") else "") + "LPR nao testada nesta rodada"
    else:
        testa_lpr(p, rel)

    # ---- rele auxiliar ----------------------------------------------------
    rele = lambda: p.get("/api/status")["saidas"]["rele"]["acionado"]
    r0 = rele()
    p.post("/api/cmd", {"id": "rele", "acao": "toggle"}); time.sleep(1)
    r1 = rele()
    p.post("/api/cmd", {"id": "rele", "acao": "toggle"}); time.sleep(1)
    r2 = rele()
    rel.item("rele auxiliar alterna e volta", r0 != r1 and r2 == r0, f"{r0} -> {r1} -> {r2}")

    # ---- SNMP --------------------------------------------------------------
    res = snmp_get(a.ip, COMMUNITY, ["1.3.6.1.4.1.66420.1.3.2.0", "1.3.6.1.4.1.66420.1.3.10.0"])
    ok = "1.3.6.1.4.1.66420.1.3.2.0" in res
    rel.item("SNMP responde (tensao da bateria, saude)", ok,
             f"bateria {res.get('1.3.6.1.4.1.66420.1.3.2.0')} mV, saude {res.get('1.3.6.1.4.1.66420.1.3.10.0')} %" if ok else "sem resposta na porta 161 (community neonex-facial)")

    testa_saidas_mcu(p, rel)
    if a.corte: testa_corte(p, rel, a.ip)

    if a.desligar:
        fim(rel, serial, meta); desliga_no_banco(p, rel); sys.exit(1 if rel.falhas() else 0)

    # ---- botao DEFAULT (manual) ---------------------------------------------
    if a.botao:
        up0 = p.get("/api/device")["uptimeSeconds"]
        rel.log(">> Segure o botao DEFAULT por 3 s (buzzer bipa, depois bip longo e a placa reinicia). Esperando ate 90 s...")
        ini = time.time(); ok = False
        while time.time() - ini < 90:
            time.sleep(3)
            try:
                p.tok = None; p.senha = None
                if p.login(["Neonex@123"]):
                    up = p.get("/api/device")["uptimeSeconds"]
                    if up < up0 and up < 60: ok = True; break
            except Exception: pass
        rel.item("botao DEFAULT: reiniciou e voltou com a senha de fabrica", ok)

    fim(rel, serial, meta)
    sys.exit(1 if rel.falhas() else 0)

PLANILHA = os.path.join(PROJ, "placas.csv") if PRODUCAO else os.path.join(PROJ, "Docs", "bancada", "placas.csv")
CAMPOS = ["etiqueta", "mac", "serial", "firmware", "data", "resultado", "itens_reprovados", "observacoes", "relatorio"]

def planilha_atualiza(linha):
    """Uma linha por placa (chave: MAC); a ultima rodada substitui a anterior."""
    import csv
    os.makedirs(os.path.dirname(PLANILHA), exist_ok=True)
    linhas = []
    if os.path.exists(PLANILHA):
        with open(PLANILHA, newline="") as f:
            linhas = [r for r in csv.DictReader(f, delimiter=";")]
    antiga = next((r for r in linhas if r.get("mac") == linha["mac"] and linha["mac"]), None)
    if antiga and antiga.get("observacoes"):
        # observacoes acumulam: o que ja estava registrado nao se perde numa rodada nova
        velhas = [o.strip() for o in antiga["observacoes"].split(",") if o.strip()]
        novas = [o.strip() for o in (linha.get("observacoes") or "").split(",") if o.strip()]
        linha["observacoes"] = ", ".join(velhas + [o for o in novas if o not in velhas])
    if antiga and not linha.get("etiqueta"): linha["etiqueta"] = antiga.get("etiqueta", "")   # rodada so pelo MAC mantem a etiqueta
    linhas = [r for r in linhas if r.get("mac") != linha["mac"] or not linha["mac"]]
    linhas.append(linha)
    linhas.sort(key=lambda r: (r.get("etiqueta") or "~", r.get("mac") or ""))
    with open(PLANILHA, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, delimiter=";")
        w.writeheader(); w.writerows(linhas)

def git(*args):
    r = subprocess.run(["git", "-C", PROJ] + list(args), capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()

def fim(rel, serial, meta=None):
    f = rel.falhas()
    rel.log("== RESULTADO: " + ("APROVADA" if not f else f"REPROVADA em {len(f)} item(ns)") + " ==")
    for nome, _, det in f: rel.log(f"   - {nome}: {det}")
    if PRODUCAO and os.path.isdir(os.path.join(PROJ, ".git")):
        rc, out = git("pull", "--rebase", "-q")        # outra bancada pode ter gravado antes
        if rc != 0: print("aviso: git pull falhou (segue sem sincronizar): " + out.splitlines()[-1] if out else "aviso: git pull falhou")
    pasta = REL_DIR; os.makedirs(pasta, exist_ok=True)
    nome = os.path.join(pasta, f"{serial}_{time.strftime('%Y%m%d-%H%M')}.log")
    open(nome, "w").write("\n".join(rel.linhas) + "\n")
    print(f"relatorio: {os.path.relpath(nome, PROJ)}")
    if meta:
        planilha_atualiza({
            "etiqueta": meta.get("etiqueta", ""), "mac": meta.get("mac", ""), "serial": meta.get("serial", ""),
            "firmware": meta.get("firmware", ""), "data": time.strftime("%Y-%m-%d %H:%M"),
            "resultado": "aprovada" if not f else "reprovada",
            "itens_reprovados": " | ".join(n for n, _, _ in f),
            "observacoes": meta.get("obs", ""),
            "relatorio": os.path.relpath(nome, PROJ)})
        print(f"planilha:  {os.path.relpath(PLANILHA, PROJ)}")
        if PRODUCAO and os.path.isdir(os.path.join(PROJ, ".git")):
            git("add", os.path.relpath(nome, PROJ), os.path.relpath(PLANILHA, PROJ))
            msg = f"placa {meta.get('etiqueta') or '(sem etiqueta)'} {meta.get('mac', '')}: " + ("aprovada" if not f else "reprovada")
            rc, out = git("commit", "-q", "-m", msg)
            if rc == 0:
                rc, out = git("push", "-q")
                print("enviado ao GitHub" if rc == 0 else "aviso: git push falhou (o commit fica local; tente 'git push' depois): " + (out.splitlines()[-1] if out else ""))

if __name__ == "__main__":
    main()
