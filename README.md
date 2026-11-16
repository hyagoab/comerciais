# hyagocomerciais

Mini site (hyagocomerciais.vercel.app) e robô de postagem do @hyagocomerciais.

- `index.html`, `trabalhos.html`, `assets/`, `videos/`, `data/trabalhos.json`: site estático publicado pela Vercel.
- `agenda/*.json`: postagens programadas (Reels + Story + trabalho no site).
- `midia/`: capas dos Reels. Os vídeos do Instagram ficam no release `midia`.
- `.github/workflows/publicar.yml`: roda a cada 10 min e programa na Zernio e coloca no site o que já foi ao ar.

Segredos do repositório: `ZERNIO_API_KEY` (chave da API da Zernio, que publica no Instagram).
