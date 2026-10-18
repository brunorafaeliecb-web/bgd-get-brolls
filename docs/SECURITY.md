---
type: documentation
status: current
created: 2026-09-15
updated: 2026-10-17
tags: [get-brolls]
---

# Segurança e privacidade

Não comite `.env`, originais, manifestos privados ou exports de revisão de clientes. Revise a árvore antes de publicar. O servidor de exemplo escuta apenas localhost. Não distribua `.venv/`, `.tools/`, perfis de navegador, cookies ou pares CDN assinados. Os instaladores obtêm as dependências nos registros oficiais.

Projetos/JSON são dados locais confiáveis. Não execute a skill como serviço público aceitando URLs, caminhos ou manifests arbitrários. O transporte HTTP interno de APIs/downloads resolve e valida todos os IPs antes da conexão, conecta aos IPs validados sem uma segunda resolução, mantém certificado/hostname HTTPS e bloqueia redirects. Ele não usa proxies automáticos do ambiente/sistema. Essa proteção não é um isolamento de rede de processos externos como yt-dlp, FFmpeg ou navegador.

A revisão não autentica quem clicou: importação exige atribuição humana com `--by`. O importador confere assinatura do conteúdo e versão da decisão (`reviewEpoch`) para impedir que exports antigos substituam decisões posteriores. Arquivos sem versão da decisão devem ser regenerados pelo Storyboard; não edite o JSON para contornar uma recusa.

## Para onde os dados vão

| Momento | Destinos | O que trafega |
|---|---|---|
| Instalação | PyPI (conjunto fixado em `requirements.txt`), registro npm (`package-lock.json`), Chrome/navegador opcional em passo separado | Apenas download das dependências. `npm ci --ignore-scripts` não baixa navegador nem executa scripts de pacote. |
| Execução | CDNs de YouTube, TikTok e Instagram via yt-dlp/curl; APIs de Pexels, Pixabay, Wikimedia Commons e NASA | URL solicitada, termos de busca e, quando existir, a chave do banco escolhido. |
| Nunca sai | Projetos, originais importados, JSON de revisão, chaves, `.env`, sessões e pares CDN assinados | Permanecem no disco local; nenhuma dessas informações é enviada a terceiros pela skill. |

Sem telemetria: a skill não envia dados a nenhum serviço próprio. Não há endpoint do autor, coleta de uso ou relatório automático de erro; todo tráfego sai para a fonte que você escolheu ou para os registros oficiais de dependências.

## `.env` e caminhos que viram execução

O `.env` que vale é, nesta ordem: `--env-file`, `GB_ENV_FILE` (só no ambiente do processo, nunca dentro de um `.env`), o `.env` da pasta da instalação (checkout) e, por fim, `$GB_HOME/.env`. Só um é lido; os dois nunca se misturam. Quem escreve em qualquer um deles escolhe executáveis e pastas de runtime: `GB_VENV_PATH` e os `GB_*_PATH` apontam o yt-dlp/ffmpeg/ffprobe, e `GB_RUNTIME_DIR` aponta a pasta com `.venv/` e `.tools/` de onde saem o yt-dlp e o Playwright. `GB_RUNTIME_DIR` pode vir de `GB_ENV_FILE` ou do `.env` do checkout, o mesmo vetor de `GB_VENV_PATH`: trate esses arquivos (e a pasta da instalação) com a mesma confiança que os próprios executáveis. Sem `GB_RUNTIME_DIR`, o runtime (do pacote, do checkout e do plugin; os instaladores só chamam o `setup`) vive em `$GB_HOME/runtime/<versão das dependências>/`: a pasta tem a mesma confiança que um executável seu.

O `setup` roda o pip (versões fixadas em `requirements.txt`, sem hashes nesta versão) e o `npm ci --ignore-scripts` com argumentos fixos, nunca por um shell. O ambiente desses processos filhos não leva variáveis terminadas em `_API_KEY`, `_TOKEN` ou `_SECRET`, nem `PYTHONPATH`/`PYTHONHOME`; a única exceção é o `NPM_TOKEN`, entregue só ao `npm ci` (o `.npmrc` de um registro privado o lê). A pasta `runtime/<versão>` que é link simbólico ou junção é recusada: o `setup` não instala no destino do link.

## Perfil `getbrolls.toml`

O perfil é configuração que vira execução: `tools.ffmpeg`, `tools.ffprobe`, `tools.ytdlp` e `tools.venv` passam a ser os executáveis (e a venv) que a CLI roda, e `runtime_dir` aponta a pasta de onde saem o yt-dlp e o Playwright. Por isso ele só vale com confiança:

- **Quem confia.** `GB_PROFILE` no ambiente do processo (a mesma fronteira de `GB_VENV_PATH`), o `$GB_HOME/getbrolls.toml` exato, ou um `profile --action trust --yes --expect <sha256>` que grava o sha dos bytes em `trusted-profiles.json`. Mudou um byte, deixa de valer até nova confiança.
- **Onde mora a confiança.** O `trusted-profiles.json` fica no `GB_HOME` de antes do perfil, nunca no `home` que o próprio perfil escolhe: senão ele se autoconfiaria.
- **Filhos.** A CLI passa aos processos filhos o perfil já conferido em `GB_PROFILE` + `GB_PROFILE_SHA256`; o filho confia nele como veio do ambiente, mas recusa se o sha não bater mais com o arquivo (editado no meio do caminho). Sem perfil válido, o filho recebe `GB_PROFILE=off`, nunca a busca de novo.
- **Recusas.** O `getbrolls.toml` nunca é link simbólico, é um arquivo comum e, no POSIX, é do usuário atual e não é gravável por grupo nem por outros.
- **O `home` relativo.** Um `home` (ou `runtime_dir`, `cache_dir`) relativo pode cair dentro do repositório onde o toml mora. O sha do perfil cobre só o toml: quem grava no repositório poderia deixar depois um `.env` (que escolhe executáveis) ou um `plugins.json` (que habilita plugins) nessa pasta. Mitigação: a prévia do `profile --action trust` avisa em `warnings` quando uma dessas pastas fica dentro da pasta do toml (e quando `plugins` cita id não instalado), e, com o `GB_HOME` vindo do perfil, o `.env` e o `plugins.json` de lá passam pelas mesmas recusas do toml (link, outro dono, gravável por grupo ou outros). Prefira pastas fora do repositório.
- **Sem confiança**, só os comandos que diagnosticam (`doctor`, `profile`, `capabilities`, `setup --check`/`--where`) rodam, relatando o problema; o resto para com código `2` antes de tocar em nada.

## Arquivos de fora (leitor único)

O que o getbrolls lê de uma pasta que outra pessoa ou outro programa escreveu (a volta de um ambiente em `handoffs/<ambiente>/NNN/`, um arquivo que um comando copia para o projeto, a pasta de `--project-dir`) passa por um leitor só, `scripts/getbrolls/reader.py`. Ele desce da raiz que o core escolheu um nome por vez e recusa link ou junção em qualquer pedaço do caminho; abre só arquivo comum, sem seguir link (no macOS e no Linux, pasta por pasta pelo descritor; no Windows, conferindo cada pedaço e a identidade do arquivo aberto, o que ainda deixa uma janela de corrida); recusa texto com mais de um nome no disco (hardlink), arquivo acima do teto (256 KiB para texto, 64 KiB para JSON, 5 MB para um componente) sem ler além dele, texto fora de UTF-8 ou com NUL e JSON com chave repetida, NaN ou aninhamento fundo. Uma pasta sem permissão de leitura é erro de E/S (`IO_ERROR`), nunca "não existe". A recusa sai com `reason_code` `UNSAFE_INPUT`.

A cópia para o projeto sai do mesmo descritor aberto, com o sha256 contado no mesmo passe: o arquivo gravado é exatamente o que foi lido, e nunca vai por cima de outro. Texto de fora que chega a uma saída (o que a volta de um ambiente diz, por exemplo) vira `untrusted_text`: um parágrafo de até 4 KiB, sem caractere de controle nem de direção (bidi), sem segredo, URL ou caminho absoluto, sempre com a origem. É dado, nunca ordem.

## Plugins

Plugins do SDK (`docs/SDK.md`) são opt-in por id: nada em `$GB_HOME/plugins/`
roda sem um `plugins --action enable --id <id> --yes` explícito, e habilitar
grava um pin de hash sobre toda a pasta — qualquer mudança de conteúdo
suspende o plugin até nova revisão. As permissões declaradas no manifesto
(`permissions.network` e `permissions.env`) são conferidas nos próprios canais
do SDK (`api.get_json`, `api.env`), não numa camada de isolamento do processo.
Os ids dos plugins oficiais (`hyperframes`, `davinci`, `asr`, `analysis_vision`, `analysis_audio`, `analysis_ocr` e `analysis_game`) e o prefixo `<oficial>_` são recusados em qualquer pasta de plugins e em qualquer índice de marketplace: um plugin de terceiros não se passa por oficial. A ponte oficial (hoje `hyperframes`) vem no pacote, em `plugins_official/`, e só carrega se o hash da pasta for o que o core fixou em `official_pins.py`; ela não passa pelo `plugins.json` e nenhuma ação de `plugins` a liga, desliga ou remove (só `GB_PLUGINS` e o perfil a desligam). A garantia é o pin (o hash da pasta que veio no pacote, gerado dentro do core) mais a origem do wheel: só o código oficial que o pacote leva é aceito, e uma cópia do usuário com id oficial fica `invalid` e nunca roda. Esse pin protege contra edição ou cópia acidental, arquivo a mais, link, bytecode fora do que o instalador compila e uma pasta de mesmo id em `$GB_HOME/plugins`; não protege contra quem pode gravar na pasta do pacote instalado, que já pode editar o próprio core. Não há caixa de areia: o código oficial roda com as permissões de quem executa a CLI. O teste `tests/test_official_plugins_purity.py` é um apoio de revisão que roda na suíte e recusa as famílias de fuga conhecidas (imports, nomes que abrem arquivo ou rodam código, atributos de frame, membros privados ou fora da lista do objeto `api`); ele não é uma caixa de areia nem uma garantia. O `.env` só entrega a um plugin variáveis do espaço de nomes dele (`<ID>_...`),
nunca uma do core ou de outro plugin, e essa variável nunca é exportada: só
`api.env` do plugin dono a lê, e subprocessos (git, ffmpeg, yt-dlp, playwright),
OpenSSL e o `ssl` do Python não a enxergam. Um nome que ferramentas do sistema
leem (`GIT_*`, `XDG_*`, `OPENSSL_*`, proxies...) só gera aviso, porque o valor
não chega ao ambiente.
**O SDK não é uma caixa de areia**: um plugin habilitado roda com as mesmas
permissões de quem executa a CLI, e não tem egress próprio além do que o
código dele fizer — só habilite plugins cujo código você leu e em que confia.

Rotas de plugin trazem o arquivo, mas o core decide o resto: a rota grava só na
pasta de trabalho que o core cria em `.getbrolls-sources/` (e apaga ao fim), e o
arquivo devolvido é recusado se estiver fora dela, for link simbólico, vazio,
maior que 512 MB ou ilegível pelo `ffprobe`. Rota que consome licença ou cota
(`stage="fetch"`) nunca roda em `inspect`, `preview` ou varredura — só no
`fetch`, depois da aprovação humana e do `permit`. `api.download` segue o
transporte do core (HTTPS, IP público, sem redirect, host em
`permissions.network`) e aceita header de autorização e URL assinada sem
registrá-los em log nem em mensagem de erro. `api.local_file` só lê arquivos
dentro de `permissions.paths` (pastas específicas — nunca `/`, a raiz de uma
unidade, `~` nem a pasta pessoal inteira), sem seguir link e sempre copiando. No
macOS e no Linux, a pasta de `permissions.paths` é aberta descendo de `/` pelo
caminho gravado no pin no `enable` (mostrado na prévia), sem seguir link; uma
pasta que não confere mais com a gravada fica ignorada, com aviso. No Windows
vale a conferência antiga, que ainda deixa uma janela de corrida, e uma junction
não exige administrador. `api.local_file` aceita hardlink e o registra no log;
os resolvedores o recusam. `permissions.paths` e `permissions.network` aparecem
no preview do `enable` e do `install`. A resposta de `api.get_json` de um plugin
nunca vai para o cache em disco. Comandos de plugin (`getbrolls x`) só leem o projeto,
por cópias. `install`/`update` fixam todo repositório git por commit (sha
completo de 40 caracteres, buscado sozinho com `fetch --depth 1`), nunca fazem
`checkout` e escrevem cada arquivo a partir do blob cru, sem filtro, hook nem
`autocrlf`. O git roda com `GIT_TERMINAL_PROMPT=0`, sem nenhuma variável `GIT_*`
herdada, só com os transportes `https` e `ssh` (e `file` apenas quando a origem
é uma pasta local indicada pela pessoa; `ext::` é recusado), com
`transfer.fsckObjects=true`, sem template de `init` e sem hooks. `--commit`,
`--ref` e `--subdir` são validados antes de qualquer git rodar (nada que comece
com `-`, sem `..`, sem pasta de VCS). São recusados: URL com credencial, query
ou fragmento; link simbólico e submódulo; pasta de controle de versão fora do
topo; ponteiro do Git LFS (o LFS nunca roda); e nome de arquivo fora de NFC. Nenhum
código do plugin roda no install. Limites do sha256 do pin: os bits de modo
(executável ou não) não entram na conta, e lixo de SO (`.DS_Store`,
`Thumbs.db`, `desktop.ini`) commitado no repositório é materializado mas não
entra no hash — o código do plugin não deve ler esses nomes. `plugins --action remove`
apaga a pasta do plugin e o estado dele em `plugins.json`; quando `plugins/<id>`
é um link, só o link sai, e o alvo não é tocado. `GB_PLUGINS=off` desliga tudo; `GB_PLUGINS=id1,id2`
só escolhe entre os plugins já habilitados com pin válido — nunca carrega um
plugin não habilitado nem um com conteúdo mudado desde o `enable`.

Marketplaces de plugins são índices fixados por commit: o índice é lido só no
commit gravado e o cache em `$GB_HOME/marketplaces/` é conferido por sha256 a
cada leitura. Instalar pelo marketplace (`install --id <id>@<marketplace>`)
não confia no índice para nada além de dizer de onde vem o plugin e o que ele
tem que ser: o conteúdo é materializado como num `--source` e recusado se o
sha256 não for o `content_sha256` da entrada ou se o manifesto divergir dela
(permissões, contribuições, compatibilidade, licença). Entrada retirada
(`yanked`) é recusada, e o perfil de workspace pode limitar os marketplaces
aceitos. O `tier` da entrada é o que o índice **declara**: qualquer marketplace
pode escrever `official`. Só o índice oficial (nome `getbrolls-plugins` e origem
fixada num dos repositórios oficiais) sai com `tier_verified: true`; nos outros a
saída diz "declarado pelo marketplace, não verificado", e `marketplace-add` de
outra origem avisa. O tier nunca libera uma conferência. Para um plugin sem
permissão, o índice pré-preenche o `--expect`: um agente que copia esse valor
pula a leitura humana. Instalar um plugin que pede qualquer permissão, ou um
`update` que acrescenta permissão, exige outro `--expect`: um valor derivado do
sha256 do conteúdo e das permissões, que o índice não traz. Ele não é segredo —
um agente consegue calculá-lo —; atrapalha quem copia o sha do índice, mas não
garante leitura humana. O que orienta mostrar a prévia à pessoa é a prévia em
chamada separada (o `install` sem `--yes` sempre para nela) e a skill.
`update --all` só lista; nada é atualizado em lote. `marketplace-update` só
aceita um commit do índice que não descende do fixado (volta atrás, história
reescrita) com `--allow-rollback`. Um commit fixado com `ref` que não é a ponta
dela vira aviso na prévia: na rede de forks do GitHub, um commit de um fork
também é servido pelo sha no repositório original, então o sha sozinho não prova
de que repositório o código veio. Quem mantém o marketplace escolhe o que
entra no índice; ele não revisa nem assina o código — continue lendo o código
de quem você habilita.

Exceção levantada por código de plugin aparece só pelo tipo — o texto dela
(que pode carregar um token) não chega à mensagem, ao `diagnostics.jsonl` nem
ao log; só `PluginError`, saneado e com os valores de `permissions.env`
trocados por `[REDACTED]`, mostra texto. O plugin também não escreve evidência
de direitos: `ORIGEM.md` e `credits.md` só trazem o que o humano registrou no
`permit` e a licença que o core anotou da rota de `fetch`, cada campo numa
linha. Chamada de plugin não tem tempo limite: um plugin travado segura o
comando até Ctrl+C.

Para reportar vulnerabilidades, use o relatório privado do GitHub em **Security → Report a vulnerability**, habilitado neste repositório. Não publique segredos ou dados de clientes em issues públicas. Nenhum endereço de contato é presumido neste pacote.
