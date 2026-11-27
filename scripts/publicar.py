#!/usr/bin/env python3
"""Robô de postagem do @hyagocomerciais (via Zernio).

Fluxo de cada arquivo em agenda/*.json:
  status "agendado"   -> envia os vídeos para a Zernio e programa o Reels e o Story
                         para o horário exato (publicar_em). O vídeo do Reels também é
                         programado no TikTok, com a mesma legenda (o Story fica só no
                         Instagram). Se o TikTok falhar, o erro fica em "tiktok" e o
                         Instagram segue normal. Para não mandar ao TikTok: "tiktok": false.
                         Post só no TikTok: sem "reels"/"story" e com
                         "tiktok": {"video_url": ..., "legenda": ...}.
                         Fica "programado".
  status "programado" -> quando o horário passa, adiciona o comercial 16:9 em
                         data/trabalhos.json (o site na Vercel atualiza sozinho) e
                         confere na Zernio se os posts saíram. Fica "publicado".
Cada etapa concluída é gravada no próprio arquivo, então nada é enviado duas vezes.

Segredo: ZERNIO_API_KEY.  Uso: python3 scripts/publicar.py [--teste]
(--teste só confere a chave e lista as contas conectadas, inclusive o TikTok; não publica nada)
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


def contas(plataforma):
    todas = lista(chamar("GET", "/accounts"), "accounts")
    return [c for c in todas if str(c.get("platform", "")).lower() == plataforma]


def conta_tiktok():
    tt = contas("tiktok")
    for c in tt:
        if "hyagocomerciais" in json.dumps(c).lower():
            return c
    return tt[0] if tt else None


def id_conta(conta):
    return conta.get("_id") or conta.get("id")


def id_perfil(conta):
    pid = conta.get("profileId")
    if isinstance(pid, dict):
        pid = pid.get("_id") or pid.get("id")
    return pid


def conta_instagram():
    ig = contas("instagram")
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
    pid = id_perfil(conta)
    if pid:
        corpo["profileId"] = pid
    r = chamar("POST", "/posts", corpo)
    post = r.get("post", r)
    return post.get("_id") or post.get("id")


def programar_tiktok(conta, quando, video_url, legenda, capa=None):
    """Post separado do Instagram, para um erro no TikTok não afetar o Reels."""
    info = chamar("GET", f"/accounts/{id_conta(conta)}/tiktok/creator-info")
    info = info.get("data", info) if isinstance(info, dict) else {}
    niveis = [n if isinstance(n, str) else (n.get("value") or n.get("level"))
              for n in (info.get("privacyLevels") or [])]
    if niveis and "PUBLIC_TO_EVERYONE" not in niveis:
        raise RuntimeError(f"A conta do TikTok não permite post público (níveis: {niveis})")
    ajustes = {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True, "allow_duet": True,
               "allow_stitch": True, "content_preview_confirmed": True, "express_consent_given": True}
    if capa:
        ajustes["video_cover_image_url"] = capa
    corpo = {"content": (legenda or "")[:2200],
             "platforms": [{"platform": "tiktok", "accountId": id_conta(conta)}],
             "mediaItems": [{"type": "video", "url": video_url}],
             "tiktokSettings": ajustes, "scheduledFor": quando, "timezone": FUSO}
    pid = id_perfil(conta)
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
        campos = ("_id", "id", "platform", "username", "displayName", "name", "profileId", "accountType",
                  "isActive", "connectionType")
        todas = lista(chamar("GET", "/accounts"), "accounts")
        print(f"Chave OK. {len(todas)} conta(s) conectada(s) na Zernio:")
        for c in todas:
            print(" -", json.dumps({k: c.get(k) for k in campos if k in c}, ensure_ascii=False))
        tt = conta_tiktok()
        if tt:
            try:
                info = chamar("GET", f"/accounts/{id_conta(tt)}/tiktok/creator-info")
                info = info.get("data", info)
                print("TikTok creator-info:", json.dumps({k: info.get(k) for k in
                      ("creator", "privacyLevels", "postingLimits") if k in info}, ensure_ascii=False)[:1500])
            except Exception as e:
                print("TikTok creator-info falhou:", e)
        else:
            print("Nenhuma conta do TikTok conectada.")
        if True:  # --teste também lista os posts recentes (só leitura)
            for fonte in ("zernio", "external"):
                for plat in ("instagram", "tiktok"):
                    try:
                        q = f"/posts?platform={plat}&limit=12&source={fonte}&view=full&sortBy=createdAt"
                        ps = lista(chamar("GET", q), "posts")
                    except Exception as e:
                        print(f"Posts {plat}/{fonte}: falhou: {e}")
                        continue
                    print(f"=== Posts {plat} ({fonte}): {len(ps)}")
                    for x in ps:
                        midia = [m.get("url") for m in (x.get("mediaItems") or [])]
                        plats = [{k: pl.get(k) for k in ("platform", "status", "platformPostUrl", "publishedAt")
                                  if pl.get(k)} for pl in (x.get("platforms") or [])]
                        print(json.dumps({"id": x.get("_id") or x.get("id"), "status": x.get("status"),
                                          "scheduledFor": x.get("scheduledFor"), "publishedAt": x.get("publishedAt"),
                                          "createdAt": x.get("createdAt"), "plataformas": plats, "midia": midia,
                                          "legenda": x.get("content")}, ensure_ascii=False))
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
                tk = item.get("tiktok")
                # Vídeo do TikTok: o do próprio bloco "tiktok" (post só no TikTok) ou o do Reels.
                tk_video = (tk or {}).get("video_url") or (r or {}).get("video_url")
                if tk_video and tk is not False and not (tk or {}).get("post_id"):
                    tk = item["tiktok"] = tk if isinstance(tk, dict) else {}
                    try:
                        ct = conta_tiktok()
                        if not ct:
                            raise RuntimeError("Nenhuma conta do TikTok conectada na Zernio.")
                        tk["post_id"] = programar_tiktok(ct, horario, tk_video,
                                                         tk.get("legenda") or (r or {}).get("legenda"),
                                                         capa=tk.get("capa_url") or (r or {}).get("capa_url"))
                        tk.pop("erro", None)
                        print("  TikTok programado:", tk["post_id"])
                    except Exception as e:
                        tk["erro"] = str(e)[:800]
                        print("  TikTok com erro (Instagram segue normal):", e)
                    salvar(caminho, item)
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
                for k in ("reels", "story", "tiktok"):
                    if isinstance(item.get(k), dict) and item[k].get("post_id"):
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
