#!/usr/bin/env python3
"""Robô de postagem do @hyagocomerciais (via Zernio).

Fluxo de cada arquivo em agenda/*.json:
  status "agendado"   -> envia os vídeos para a Zernio e programa o Reels e o Story
                         para o horário exato (publicar_em). Fica "programado".
  status "programado" -> quando o horário passa, adiciona o comercial 16:9 em
                         data/trabalhos.json (o site na Vercel atualiza sozinho) e
                         confere na Zernio se os posts saíram. Fica "publicado".
Cada etapa concluída é gravada no próprio arquivo, então nada é enviado duas vezes.

Segredo: ZERNIO_API_KEY.  Uso: python3 scripts/publicar.py [--teste]
(--teste só confere a chave e lista as contas conectadas; não publica nada)
"""
import json, os, sys, glob, subprocess, urllib.request, urllib.error
from datetime import datetime, timezone

API = "https://zernio.com/api/v1"
CHAVE = os.environ.get("ZERNIO_API_KEY", "").strip()
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUSO = "America/Sao_Paulo"


def chamar(metodo, caminho, corpo=None):
    url = caminho if caminho.startswith("http") else API + caminho
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo, headers={
        "Authorization": f"Bearer {CHAVE}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            txt = r.read().decode() or "{}"
            return json.loads(txt)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Zernio {metodo} {caminho} -> HTTP {e.code}: {e.read().decode(errors='replace')[:800]}")


def lista(resp, *chaves):
    if isinstance(resp, list):
        return resp
    for k in chaves + ("data", "items", "results"):
        if isinstance(resp.get(k), list):
            return resp[k]
    return []


def conta_instagram():
    contas = lista(chamar("GET", "/accounts"), "accounts")
    ig = [c for c in contas if str(c.get("platform", "")).lower() == "instagram"]
    if not ig:
        raise RuntimeError("Nenhuma conta do Instagram conectada na Zernio.")
    for c in ig:
        if "hyagocomerciais" in json.dumps(c).lower():
            return c
    return ig[0]


def baixar(ref):
    if ref.startswith("release:"):
        nome = ref.split(":", 1)[1]
        destino = os.path.join("/tmp", nome)
        if not os.path.exists(destino):
            subprocess.run(["gh", "release", "download", "midia", "-p", nome, "-D", "/tmp", "--clobber",
                            "-R", os.environ["GITHUB_REPOSITORY"]], check=True)
        return destino
    return os.path.join(RAIZ, ref)


def enviar_midia(caminho):
    nome = os.path.basename(caminho)
    p = chamar("POST", "/media/presign", {"filename": nome, "contentType": "video/mp4"})
    with open(caminho, "rb") as f:
        dados = f.read()
    req = urllib.request.Request(p["uploadUrl"], data=dados, method="PUT", headers={"Content-Type": "video/mp4"})
    with urllib.request.urlopen(req, timeout=900) as r:
        if r.status not in (200, 201, 204):
            raise RuntimeError(f"Envio do vídeo falhou: HTTP {r.status}")
    return p["publicUrl"]


def programar(conta, quando, video_url, legenda=None, story=False, capa=None):
    plataforma = {"platform": "instagram", "accountId": conta.get("_id") or conta.get("id")}
    if story:
        plataforma["platformSpecificData"] = {"contentType": "story"}
    item = {"type": "video", "url": video_url}
    if capa:
        item["instagramThumbnail"] = capa
    corpo = {"content": legenda or "", "platforms": [plataforma], "mediaItems": [item],
             "scheduledFor": quando, "timezone": FUSO}
    pid = conta.get("profileId")
    if isinstance(pid, dict):
        pid = pid.get("_id") or pid.get("id")
    if pid:
        corpo["profileId"] = pid
    r = chamar("POST", "/posts", corpo)
    post = r.get("post", r)
    return post.get("_id") or post.get("id")


def salvar(caminho, item):
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)
        f.write("\n")


def adicionar_ao_site(site):
    arq = os.path.join(RAIZ, "data", "trabalhos.json")
    trabalhos = json.load(open(arq, encoding="utf-8"))
    trabalhos = [t for t in trabalhos if t.get("id") != site["id"]]
    trabalhos.insert(0, site)
    with open(arq, "w", encoding="utf-8") as f:
        json.dump(trabalhos, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    if not CHAVE:
        sys.exit("ZERNIO_API_KEY não configurada nos segredos do repositório.")
    if "--teste" in sys.argv:
        c = conta_instagram()
        resumo = {k: c.get(k) for k in ("_id", "id", "platform", "username", "displayName", "name", "profileId",
                                         "accountType", "isActive") if k in c}
        print("Chave OK. Conta do Instagram na Zernio:", json.dumps(resumo, ensure_ascii=False))
        return
    agora = datetime.now(timezone.utc)
    for caminho in sorted(glob.glob(os.path.join(RAIZ, "agenda", "*.json"))):
        item = json.load(open(caminho, encoding="utf-8"))
        st = item.get("status")
        if st not in ("agendado", "programado"):
            continue
        quando = datetime.fromisoformat(item["publicar_em"])
        print(f"→ {item['id']} ({st}, {item['publicar_em']})")
        try:
            if st == "agendado":
                conta = conta_instagram()
                # Se o horário já passou, programa para daqui a 2 minutos.
                alvo = max(quando, agora)
                if alvo == agora:
                    from datetime import timedelta
                    alvo = agora + timedelta(minutes=2)
                horario = alvo.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                r = item.get("reels")
                if r and not r.get("post_id"):
                    url = r.get("video_url") or enviar_midia(baixar(r["arquivo"]))
                    r["video_url"] = url
                    salvar(caminho, item)
                    r["post_id"] = programar(conta, horario, url, legenda=r["legenda"], capa=r.get("capa_url"))
                    salvar(caminho, item)
                    print("  Reels programado:", r["post_id"])
                s = item.get("story")
                if s and not s.get("post_id"):
                    url = s.get("video_url") or enviar_midia(baixar(s["arquivo"]))
                    s["video_url"] = url
                    salvar(caminho, item)
                    s["post_id"] = programar(conta, horario, url, story=True)
                    salvar(caminho, item)
                    print("  Story programado:", s["post_id"])
                item["status"] = "programado"
                item.pop("erro", None)
            elif quando <= agora:
                for k in ("reels", "story"):
                    if item.get(k, {}).get("post_id"):
                        try:
                            p = chamar("GET", f"/posts/{item[k]['post_id']}")
                            item[k]["status_zernio"] = p.get("post", p).get("status")
                        except Exception as e:
                            item[k]["status_zernio"] = f"não consegui conferir: {e}"[:200]
                if item.get("site") and not item["site"].get("publicado"):
                    site = {k: v for k, v in item["site"].items() if k != "publicado"}
                    site.setdefault("data", quando.date().isoformat())
                    adicionar_ao_site(site)
                    item["site"]["publicado"] = True
                    print("  Trabalho adicionado ao site:", site["titulo"])
                item["status"] = "publicado"
                item["publicado_em"] = agora.isoformat(timespec="seconds")
        except Exception as e:
            item["erro"] = str(e)[:800]
            print("  ERRO:", e)
        salvar(caminho, item)


if __name__ == "__main__":
    main()
