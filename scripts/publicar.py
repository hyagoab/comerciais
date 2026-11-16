#!/usr/bin/env python3
"""Robô de postagem do @hyagocomerciais.

Lê agenda/*.json e, para cada item com status "agendado" cujo horário já chegou:
  1. publica o Reels (vídeo vertical + legenda + capa) pela API do Instagram;
  2. publica o Story (versão com a figurinha "REELS NOVO");
  3. adiciona o comercial 16:9 em data/trabalhos.json (o site na Vercel atualiza sozinho).
Cada etapa concluída fica gravada no próprio arquivo da agenda, então uma falha
no meio nunca publica nada duas vezes.

Variáveis de ambiente: META_TOKEN (obrigatória), IG_USER_ID (opcional),
GITHUB_REPOSITORY e GH_TOKEN (para baixar os vídeos do release "midia").
Uso: python3 scripts/publicar.py [--teste]   (--teste só valida a chave, não publica nada)
"""
import json, os, sys, time, glob, subprocess, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

API = "https://graph.facebook.com/v25.0"
RUP = "https://rupload.facebook.com/ig-api-upload/v25.0"
TOKEN = os.environ.get("META_TOKEN", "").strip()
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def chamar(metodo, url, dados=None, cabecalhos=None, corpo=None, tentativas=3):
    cab = {"Authorization": f"Bearer {TOKEN}"}
    cab.update(cabecalhos or {})
    if dados is not None:
        corpo = urllib.parse.urlencode(dados).encode()
        cab["Content-Type"] = "application/x-www-form-urlencoded"
    for i in range(tentativas):
        req = urllib.request.Request(url, data=corpo, headers=cab, method=metodo)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if e.code >= 500 and i < tentativas - 1:
                time.sleep(10 * (i + 1)); continue
            raise RuntimeError(f"HTTP {e.code} em {url.split('?')[0]}: {msg[:600]}")
        except urllib.error.URLError as e:
            if i < tentativas - 1:
                time.sleep(10 * (i + 1)); continue
            raise


def ig_user_id():
    if os.environ.get("IG_USER_ID"):
        return os.environ["IG_USER_ID"].strip()
    r = chamar("GET", f"{API}/me/accounts?fields=name,instagram_business_account{{id,username}}")
    for p in r.get("data", []):
        iba = p.get("instagram_business_account")
        if iba and iba.get("username", "").lower() == "hyagocomerciais":
            return iba["id"]
    for p in r.get("data", []):
        if p.get("instagram_business_account"):
            return p["instagram_business_account"]["id"]
    raise RuntimeError("Nenhuma conta do Instagram ligada às páginas desta chave.")


def baixar(ref):
    """ref = 'release:<arquivo>' (release 'midia' do repositório) ou caminho dentro do repositório."""
    if ref.startswith("release:"):
        nome = ref.split(":", 1)[1]
        destino = os.path.join("/tmp", nome)
        if not os.path.exists(destino):
            subprocess.run(["gh", "release", "download", "midia", "-p", nome, "-D", "/tmp", "--clobber",
                            "-R", os.environ["GITHUB_REPOSITORY"]], check=True)
        return destino
    return os.path.join(RAIZ, ref)


def publicar_video(ig, tipo, arquivo, extra):
    dados = {"media_type": tipo, "upload_type": "resumable"}
    dados.update(extra)
    cont = chamar("POST", f"{API}/{ig}/media", dados=dados)["id"]
    tam = os.path.getsize(arquivo)
    with open(arquivo, "rb") as f:
        corpo = f.read()
    r = chamar("POST", f"{RUP}/{cont}", cabecalhos={"Authorization": f"OAuth {TOKEN}", "offset": "0",
                                                      "file_size": str(tam)}, corpo=corpo)
    if not r.get("success", True):
        raise RuntimeError(f"Envio do vídeo falhou: {r}")
    for _ in range(90):  # até ~15 min processando
        st = chamar("GET", f"{API}/{cont}?fields=status_code,status")
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram recusou o vídeo: {st}")
        time.sleep(10)
    else:
        raise RuntimeError("Vídeo ainda processando depois de 15 min.")
    return chamar("POST", f"{API}/{ig}/media_publish", dados={"creation_id": cont})["id"]


def salvar(caminho, item):
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)
        f.write("\n")


def adicionar_ao_site(site):
    arq = os.path.join(RAIZ, "data", "trabalhos.json")
    lista = json.load(open(arq, encoding="utf-8"))
    lista = [t for t in lista if t.get("id") != site["id"]]
    lista.insert(0, site)
    with open(arq, "w", encoding="utf-8") as f:
        json.dump(lista, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    if not TOKEN:
        sys.exit("META_TOKEN não configurado nos segredos do repositório.")
    ig = ig_user_id()
    if "--teste" in sys.argv:
        u = chamar("GET", f"{API}/{ig}?fields=username,followers_count,media_count")
        lim = chamar("GET", f"{API}/{ig}/content_publishing_limit?fields=quota_usage,config")
        print("Chave OK. Conta:", u.get("username"), "| seguidores:", u.get("followers_count"),
              "| posts:", u.get("media_count"), "| limite:", json.dumps(lim.get("data", lim)))
        return
    agora = datetime.now(timezone.utc)
    mudou = False
    for caminho in sorted(glob.glob(os.path.join(RAIZ, "agenda", "*.json"))):
        item = json.load(open(caminho, encoding="utf-8"))
        if item.get("status") not in ("agendado", "publicando"):
            continue
        quando = datetime.fromisoformat(item["publicar_em"])
        if quando > agora:
            continue
        print(f"→ {item['id']} (agendado para {item['publicar_em']})")
        item["status"] = "publicando"
        try:
            if item.get("reels") and not item["reels"].get("media_id"):
                r = item["reels"]
                extra = {"caption": r["legenda"], "share_to_feed": "true"}
                if r.get("capa_url"):
                    extra["cover_url"] = r["capa_url"]
                elif r.get("capa_ms") is not None:
                    extra["thumb_offset"] = str(r["capa_ms"])
                r["media_id"] = publicar_video(ig, "REELS", baixar(r["arquivo"]), extra)
                salvar(caminho, item); mudou = True
                print("  Reels publicado:", r["media_id"])
            if item.get("story") and not item["story"].get("media_id"):
                s = item["story"]
                s["media_id"] = publicar_video(ig, "STORIES", baixar(s["arquivo"]), {})
                salvar(caminho, item); mudou = True
                print("  Story publicado:", s["media_id"])
            if item.get("site") and not item["site"].get("publicado"):
                site = {k: v for k, v in item["site"].items() if k != "publicado"}
                site.setdefault("data", agora.astimezone().date().isoformat())
                adicionar_ao_site(site)
                item["site"]["publicado"] = True
                print("  Trabalho adicionado ao site:", site["titulo"])
            item["status"] = "publicado"
            item["publicado_em"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        except Exception as e:
            item["status"] = "erro"
            item["erro"] = str(e)[:800]
            print("  ERRO:", e)
        salvar(caminho, item); mudou = True
    if not mudou:
        print("Nada para publicar agora.")


if __name__ == "__main__":
    main()
