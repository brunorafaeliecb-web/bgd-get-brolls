"""Argument contract and structured command output."""

# pylint: disable=missing-function-docstring,broad-exception-caught,too-many-lines
# `too-many-lines`: o contrato inteiro da CLI (parser, erro de uso, saída e códigos de
# saída) fica num módulo só, para quem lê a superfície não caçar em vários arquivos.
# Legado: ocorrências pré-existentes em `parse_args` e `entrypoint` (corpo
# idêntico ao código anterior à 2.6.0).

import argparse
import contextlib
import contextvars
import difflib
import errno
import json
import logging
import os
import platform
import re
import sys
import time
from collections.abc import Callable
from gettext import gettext
from pathlib import Path
from typing import NoReturn, cast

from . import __version__, _paths, labels, logs, presets, renamed, vocab
from .errors import EnvReadError, PrerequisiteError, Refusal, UsageError
from .models import now
from .renamed import Rename, message_en, refuse_at_parse
from .runtime import (
    READ_ONLY_ACTIONS,
    READ_ONLY_COMMANDS,
    SELF_LOCKED_COMMANDS,
    OperationError,
    audited,
    error_code_for,
    exit_code_for,
    is_folder,
    missing_project_tree,
    writes_installation,
)
from .sdk.scaffold import KINDS as SCAFFOLD_KINDS

# Named so a caller (script, test, or someone scripting the CLI) never has to hardcode a
# number. Which error leaves with which code is decided in one place: `runtime.ERROR_EXIT`.
EXIT_OK = 0
EXIT_OPERATION_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_INTERNAL_ERROR = 3
EXIT_PREREQUISITE = 4
EXIT_INTERRUPTED = 130
EXIT_CODES = (
    (EXIT_OK, "ok", "Deu certo"),
    (EXIT_OPERATION_ERROR, "operation", "Erro de operação ou de dados"),
    (EXIT_USAGE_ERROR, "usage", "Erro de uso: comando, flag ou configuração"),
    (EXIT_INTERNAL_ERROR, "internal", "Erro interno (bug); detalhes só em diagnostics.jsonl"),
    (EXIT_PREREQUISITE, "prerequisite", "Falta um pré-requisito da instalação"),
    (EXIT_INTERRUPTED, "interrupted", "Interrompido (Ctrl+C)"),
)
# Só estes comandos saem 4 com o resultado em stdout (`"ready": false`); nos outros, 4
# é sempre um erro de pré-requisito em JSON no stderr.
PREREQUISITE_COMMANDS = ("doctor", "setup")
_RESULT_EXIT: contextvars.ContextVar[int] = contextvars.ContextVar("getbrolls_result_exit", default=EXIT_OK)

# Uma linha por subcomando: o que ele faz no fluxo coleta → revisão → entrega.
SUMMARIES = {
    "providers": "Listar fontes disponíveis, transporte e chaves configuradas",
    "doctor": "Diagnosticar dependências, caminhos fixados e fontes utilizáveis",
    "plugins": (
        "Listar, instalar (de pasta, git ou marketplace), atualizar, remover, criar, habilitar, desabilitar ou "
        "validar plugins do SDK ($GB_HOME/plugins, padrão ~/.getbrolls/plugins), procurar nos marketplaces e "
        "gerenciá-los"
    ),
    "capabilities": (
        "Descrever em JSON os comandos, flags, códigos de saída e comandos de plugin desta instalação (para agentes)"
    ),
    "environment": (
        "Listar os ambientes que vêm com o getbrolls (pontes oficiais, hoje o HyperFrames): versão, procedência e "
        "estado, sem rodar código delas"
    ),
    "client": (
        "Registrar, listar, mostrar ou desregistrar pastas de cliente (prateleira em assets/, projetos/) no "
        "$GB_HOME/clients.json"
    ),
    "template": "Comando antigo das receitas (agora recipe): freeze, list e show só dizem o comando novo",
    "recipe": ("Listar e mostrar as receitas seladas dos clientes, conferindo a integridade de cada arquivo (só leem)"),
    "x": "Rodar um comando de plugin habilitado (x --list mostra quais existem); só lê o projeto",
    "profile": "Mostrar o perfil getbrolls.toml em vigor (valor e origem de cada campo) ou confiar/desconfiar dele",
    "setup": (
        "Instalar o runtime da instalação (yt-dlp numa venv e Playwright via npm) em $GB_HOME/runtime; "
        "--check só confere; --where mostra as pastas; FFmpeg e Node só são verificados"
    ),
    "status": "Resumir onde o projeto está por etapa, sem alterar arquivos",
    "search": "Pesquisar candidatos numa fonte e registrá-los no projeto (--shot liga ao beat; --dry-run não grava)",
    "resolve": "Registrar um candidato a partir de URL pública ou arquivo local",
    "inspect": "Analisar a fonte (duração, capítulos, legendas) antes de coletar",
    "preview": "Gerar prévia (GIF/contact sheet) do intervalo escolhido",
    "approve": "Registrar aprovação humana já recebida para o intervalo atual",
    "permit": "Registrar as condições reais de uso do trecho antes da coleta",
    "reject": "Marcar candidatos como rejeitados e invalidar suas revisões (--candidate repetível)",
    "fetch": "Produzir o corte final aprovado e permitido (broll/ no layout 1, brolls/clips/ no 0)",
    "verify": "Conferir integridade e decodificação dos arquivos coletados",
    "review": "Gerar o Storyboard local em brolls/review.html",
    "import-review": "Importar o JSON de decisões exportado pelo Storyboard",
    "init": (
        "Criar um projeto novo de layout 1: project.json, aroll/, assets/, broll/ e analysis/ "
        "(--recipe: a partir de uma receita do cliente)"
    ),
    "migrate": (
        "Adotar o layout 1 num projeto antigo: só acrescenta o project.json, sem mover nada "
        "(--action plan mostra antes; apply grava)"
    ),
    "init-rules": "Criar um RULES.md editável no projeto (--format muda o formato-alvo)",
    "rules": "Mostrar as regras editoriais em vigor no projeto",
    "init-brief": "Criar um BRIEF.md editável com o plano deste vídeo",
    "brief": "Mostrar os beats do vídeo e o comando pronto de cada um",
    "remember": "Registrar referência aprovada ou rejeitada na memória do projeto",
    "references": "Consultar as referências memorizadas do projeto",
    "learn": "Guardar busca, preferência ou trecho útil na biblioteca entre projetos",
    "library": "Consultar a biblioteca entre projetos antes de sair buscando",
    "browser-plan": "Planejar a captura de uma página pelo navegador autorizado",
    "queue": "Enfileirar URLs sociais e ditar o ritmo do lote (add, next, mark, status)",
    "serve": "Servir brolls/review.html em 127.0.0.1 para abrir o Storyboard no navegador",
    "deliver": "Organizar os trechos coletados em entrega/, uma pasta por beat",
    "roteiro": "Criar, validar, revisar e sincronizar o ROTEIRO.md com os beats do BRIEF.md",
    "assets": (
        "Listar componentes do projeto (marca, lettering, sfx, música, imagem, composições, A-ROLL) e resolver nomes"
    ),
    "export": (
        "Transformar o roteiro revisado num projeto de edição por um exporter de plugin de terceiros,"
        " numa pasta nova em exports/"
    ),
    "analysis": (
        "Registrar mídias do projeto em analysis/ (id por conteúdo) e listar ou conferir os arquivos de análise"
    ),
}

# Subcomandos que `execute()` (commands.py) de fato leva até
# `sync_formats(ledger, rules, confirm=...)`: os demais retornam antes (serve, queue,
# init-rules, init-brief, brief, learn, library, rules) ou estão em READ_ONLY_CONSULTS
# (references, inspect) — `--confirm-format-change` não tem efeito nenhum lá.
FORMAT_GATE_SUBCOMMANDS = (
    "search",
    "resolve",
    "preview",
    "approve",
    "permit",
    "reject",
    "fetch",
    "verify",
    "review",
    "import-review",
    "remember",
    "browser-plan",
    "deliver",
)


# Cor que o argparse do Python 3.14+ pode pôr no `usage`: fora do JSON.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _stderr_is_tty():
    """Stderr (para onde o erro de uso vai) num terminal? Stream fechado ou sem `isatty` conta como não."""
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _close_match(bad, candidates):
    found = difflib.get_close_matches(bad, candidates, n=1, cutoff=0.6)
    return found[0] if found else None


def _suggest_option(root, ns, extras):
    """A opção mais parecida com a primeira `--flag` desconhecida, no subcomando e na raiz."""
    bad = next((token.split("=", 1)[0] for token in extras if token.startswith("--")), None)
    if bad is None:
        return None
    parsers = [_SUBPARSERS.get(str(getattr(ns, "command", ""))), root]
    candidates = [
        option
        for parser in parsers
        if parser is not None
        for action in parser._actions  # pylint: disable=protected-access
        for option in action.option_strings
        if option.startswith("--")
    ]
    return _close_match(bad, candidates)


# Corte mais frouxo quando o candidato é uma das opções obrigatórias que faltaram:
# `library --query x` → `--search` (o que a pessoa quis dizer é o que falta).
_MISSING_REQUIRED_CUTOFF = 0.5


def _missing_required(parser, args):
    """Opções obrigatórias de `parser` que não aparecem em `args`."""
    given = {token.split("=", 1)[0] for token in args if token.startswith("--")}
    return [
        next(option for option in action.option_strings if option.startswith("--"))
        for action in parser._actions  # pylint: disable=protected-access
        if action.required and action.option_strings and not given & set(action.option_strings)
    ]


def _suggest_missing_required(parser, args):
    """Para "the following arguments are required": a opção parecida com a `--flag` desconhecida."""
    known = {option for action in parser._actions for option in action.option_strings}  # pylint: disable=protected-access
    bad = next((t.split("=", 1)[0] for t in args if t.startswith("--") and t.split("=", 1)[0] not in known), None)
    if bad is None:
        return None
    missing = _missing_required(parser, args)
    found = _close_match(bad, [option for option in known if option.startswith("--")])
    if found is None:
        close = difflib.get_close_matches(bad, missing, n=1, cutoff=_MISSING_REQUIRED_CUTOFF)
        found = close[0] if close else (missing[0] if len(missing) == 1 else None)
    return found


# Mensagens comuns do argparse em pt-BR. O original (inglês) continua em `error_en`.
# Padrões das versões 3.11–3.14; mensagem que nenhum casa sai como veio.
_PT_BR_USAGE = (
    (re.compile(r"^the following arguments are required: (?P<a>.+)$"), "faltam argumentos obrigatórios: {a}"),
    (re.compile(r"^one of the arguments (?P<a>.+) is required$"), "um destes argumentos é obrigatório: {a}"),
    (re.compile(r"^unrecognized arguments: (?P<a>.+)$"), "argumentos não reconhecidos: {a}"),
    (
        re.compile(r"^argument (?P<a>.+?): invalid choice: (?P<v>.+?) \(choose from (?P<c>.+)\)$"),
        "argumento {a}: opção inválida: {v} (escolha entre {c})",
    ),
    (re.compile(r"^argument (?P<a>.+?): expected one argument$"), "argumento {a}: falta o valor (esperava um)"),
    (
        re.compile(r"^argument (?P<a>.+?): expected at least one argument$"),
        "argumento {a}: esperava pelo menos um valor",
    ),
    (
        re.compile(r"^argument (?P<a>.+?): not allowed with argument (?P<b>.+)$"),
        "argumento {a}: não vale junto com {b}",
    ),
    (
        re.compile(r"^argument (?P<a>.+?): invalid (?P<t>\S+) value: (?P<v>.+)$"),
        "argumento {a}: valor {t} inválido: {v}",
    ),
    (re.compile(r"^ambiguous option: (?P<a>\S+) could match (?P<m>.+)$"), "opção ambígua: {a} pode ser {m}"),
)


def usage_pt_br(message):
    """A mensagem de uso do argparse em pt-BR (nomes, valores e escolhas ficam como vieram)."""
    for pattern, template in _PT_BR_USAGE:
        found = pattern.match(message)
        if found:
            return template.format(**found.groupdict())
    return message


class GbArgumentParser(argparse.ArgumentParser):
    """ArgumentParser cujo erro de uso sai em JSON fora do terminal e sugere o nome parecido.

    A mensagem (`error`) sai em pt-BR (`usage_pt_br`); a do argparse, byte a byte, fica em
    `error_en` para ferramentas. O código de saída é 2. Num terminal, sai o `usage` e
    `prog: erro: …` em pt-BR e, quando há nome parecido, uma linha "Você quis dizer: …?".
    """

    _bad_choice: tuple[str, tuple[str, ...]] | None = None
    _args: tuple[str, ...] = ()
    # Gancho do subcomando: forma antiga reconhecida nos tokens (sem o valor de cada flag que o tem)
    # → o `renamed.Rename` dela. Quem recusa é `renamed`, nunca o parser.
    renamed: Callable[[tuple[str, ...]], Rename | None] | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Os títulos dos grupos e o texto do `-h` que o argparse traz em inglês (`--help` sai em pt-BR por inteiro).
        self._positionals.title = "argumentos posicionais"
        self._optionals.title = "opções"
        self._option_string_actions["-h"].help = "mostra esta ajuda e sai"

    def format_help(self):
        """A ajuda inteira em pt-BR. A linha `usage:` do erro de uso fica como na 2.5 (o ouro pina os bytes dela)."""
        text = super().format_help().removeprefix("usage: ")
        # "uso: " tem 2 caracteres a menos que "usage: ": o recuo das linhas que continuam o uso acompanha.
        head, sep, rest = text.partition("\n\n")
        first, newline, tail = head.partition("\n")
        shifted = "\n".join(line.removeprefix("  ") for line in tail.split("\n")) if newline else ""
        return "uso: " + first + newline + shifted + sep + rest

    def parse_known_args(self, args=None, namespace=None):  # pyright: ignore[reportIncompatibleMethodOverride]
        # O argparse não passa ao `error` os tokens que sobraram; guardá-los aqui deixa o
        # erro de opção obrigatória sugerir a opção que a pessoa quase digitou.
        self._args = tuple(sys.argv[1:] if args is None else map(str, args))
        return super().parse_known_args(args, namespace)

    def _check_value(self, action, value):
        try:
            super()._check_value(action, value)
        except argparse.ArgumentError:
            self._bad_choice = (str(value), tuple(map(str, action.choices or ())))
            raise

    def parse_args(self, args=None, namespace=None):  # pyright: ignore[reportIncompatibleMethodOverride]
        self._bad_choice = None
        ns, extras = self.parse_known_args(args, namespace)
        if extras:
            self.error(
                gettext("unrecognized arguments: %s") % " ".join(extras), suggestion=_suggest_option(self, ns, extras)
            )
        return ns

    def _get_option_tuples(self, option_string):
        """Abreviações de `option_string`: a flag escondida (retirada) não disputa o prefixo com as visíveis.

        `init --tem` é `--tema`, não "`--tema` ou `--template`": o argparse listaria a flag que saiu. Um prefixo
        que só cabe em flags escondidas continua levando a elas (a recusa de `renamed`).
        """
        found = super()._get_option_tuples(option_string)  # pyright: ignore[reportAttributeAccessIssue]
        shown = [item for item in found if item[0].help != argparse.SUPPRESS]
        return shown or found

    def _option_action(self, arg):
        """A opção que `arg` nomeia (nome inteiro ou prefixo único, como o argparse aceita) ou `None`."""
        known = self._option_string_actions
        if arg in known:
            return known[arg]
        found = {id(action): action for name, action in known.items() if arg.startswith("--") and name.startswith(arg)}
        return next(iter(found.values())) if len(found) == 1 else None

    def _without_flag_values(self, args):
        """`args` sem o token que é o valor de uma flag que recebe valor (`--path trust`: o `trust` sai)."""
        kept = []
        takes_value = False
        for arg in args:
            if takes_value:
                takes_value = False
                continue
            kept.append(arg)
            if arg.startswith("-") and "=" not in arg:
                action = self._option_action(arg)
                takes_value = action is not None and action.nargs != 0
        return tuple(kept)

    def error(self, message, suggestion=None) -> NoReturn:
        found = self.renamed(self._without_flag_values(self._args)) if self.renamed is not None else None
        if suggestion is None and self._bad_choice is not None:
            suggestion = _close_match(*self._bad_choice)
        if suggestion is None and message.startswith(gettext("the following arguments are required: %s") % ""):
            suggestion = _suggest_missing_required(self, self._args)
        self._bad_choice = None
        translated = usage_pt_br(message)
        if found is not None:
            # Mesma recusa e mesmo `renamed_refused` da execução: o parser só passa o `Rename` adiante.
            translated, suggestion, message = (
                str(refuse_at_parse(found)),
                found.suggestion,
                message_en(found.old, found.new),
            )
        if _stderr_is_tty():
            self.print_usage(sys.stderr)
            sys.stderr.write(f"{self.prog}: erro: {translated}\n")
            if suggestion:
                sys.stderr.write(f"Você quis dizer: {suggestion}?\n")
        else:
            payload = {
                "error": translated,
                "error_en": message,
                "error_code": "USAGE_ERROR",
                "usage": _ANSI.sub("", self.format_usage()).strip(),
                "suggestion": suggestion,
                "prog": self.prog,
            }
            if found is not None:
                # O envelope que a execução dá à mesma recusa: qual comando, quando, e a mensagem.
                payload |= {
                    "operation": self.prog.rpartition(" ")[2],
                    "status": "error",
                    "at": now(),
                    "message": translated,
                }
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        self.exit(EXIT_USAGE_ERROR)


def version_line():
    """`getbrolls <versão> (Python <x.y.z>; dados: wheel|checkout|ausentes)`."""
    origin = _paths.origin()
    label = "ausentes" if origin == "unknown" else origin
    return f"getbrolls {__version__} (Python {platform.python_version()}; dados: {label})"


class _VersionAction(argparse.Action):
    """`--version`: imprime `version_line()` em stdout e sai com 0."""

    def __init__(self, option_strings, **kwargs):
        kwargs.setdefault("dest", argparse.SUPPRESS)
        kwargs.setdefault("default", argparse.SUPPRESS)
        super().__init__(option_strings, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        del namespace, values, option_string  # só imprime e sai
        print(version_line())
        parser.exit(0)


# Subparsers da última `build_parser()`: um erro de uso validado depois do parse
# (ex.: `permit --preset`) sai no mesmo formato do argparse daquele subcomando.
_SUBPARSERS: dict[str, argparse.ArgumentParser] = {}


def _check_preset_name(args):
    """`permit --preset <nome>` desconhecido é erro de uso, como no 2.5.0: sai
    antes de abrir, travar ou registrar o projeto — nenhum `brolls/` é criado. Nome
    embutido nem olha plugin; os de plugin vêm do manifesto (sem rodar código)."""
    name = getattr(args, "preset", None) if args.command == "permit" else None
    if not name or name in presets.PERMIT_PRESETS:
        return
    valid = presets.names()
    parser = _SUBPARSERS.get("permit")
    if name in valid or parser is None:
        return
    # O próprio argparse monta a mensagem, byte a byte como no 2.5.0 (quando `--preset`
    # tinha `choices=`), em qualquer versão do Python: aspas em cada nome e tudo.
    action = next(a for a in parser._actions if a.dest == "preset")  # pylint: disable=protected-access
    action.choices = valid
    try:
        parser._check_value(action, name)  # pylint: disable=protected-access
    except argparse.ArgumentError as exc:
        parser.error(str(exc))


def _add_client_arguments(p):
    """Flags de `client`: a pasta de cada cliente fica onde a pessoa escolher, registrada no GB_HOME."""
    p.add_argument(
        "--action",
        required=True,
        choices=["add", "list", "show", "remove"],
        help=(
            "add: cria (ou reaproveita) <root>/<slug>/ e registra; list/show: só leem o registro e o client.json; "
            "remove: desregistra, sem apagar a pasta"
        ),
    )
    p.add_argument("--slug", help="Slug do cliente: minúsculas, números e - (add/show/remove)")
    p.add_argument("--name", help="Nome de exibição do cliente novo (add); sem ele, o slug")
    p.add_argument("--root", help="Pasta existente, em caminho absoluto, onde nasce <root>/<slug>/ (add)")


def _recipe_ref(text):
    from .refs import recipe_parts

    recipe_parts(text)
    return text


def _add_template_arguments(p):
    """Flags de `template`: as receitas saíram daqui (agora `recipe`); as ações antigas só dizem o comando novo."""
    p.add_argument(
        "--action",
        required=True,
        choices=["freeze", "list", "show"],
        help="Ações da versão de testes, que agora só dizem o comando novo: freeze, list e show → recipe",
    )
    # A linha antiga inteira (`template --action freeze --from P --slug S …`) chega à recusa, nunca a
    # "argumentos não reconhecidos": as flags dela ficam aceitas e escondidas.
    for flag, dest in (("--from", "source"), ("--slug", "slug"), ("--client", "client"), ("--ref", "ref"),
                       ("--title", "title"), ("--by", "by")):  # fmt: skip
        p.add_argument(flag, dest=dest, help=argparse.SUPPRESS)
    p.add_argument("--engine-ref", dest="engine_ref", action="append", help=argparse.SUPPRESS)


def _add_recipe_arguments(p):
    """Flags de `recipe` (list e show só leem as pastas de receita dos clientes registrados)."""
    p.add_argument(
        "--action",
        required=True,
        choices=["list", "show"],
        help="list: as versões das receitas dos clientes registrados; show: uma versão, com o sha256 de cada arquivo",
    )
    p.add_argument(
        "--ref",
        type=_checked(_recipe_ref),
        help="Receita: <cliente>.<slug>[@N] ou cat:getbrolls/recipe/<cliente>.<slug>@<N>; sem @N, a última (show)",
    )


def _add_environment_arguments(p):
    """Flags de `environment` (list só lê o inventário das pontes oficiais)."""
    p.add_argument(
        "--action",
        required=True,
        choices=["list"],
        help="list: as pontes de ambiente desta instalação, com versão, sha256 do pin e estado",
    )


# Os comandos que já mostravam `--json` na ajuda. Nos outros ele é aceito e fica fora da ajuda e do
# `usage`: o erro de uso de um comando da 2.5 traz o `usage`, e o ouro da 2.5 o confere byte a byte.
_JSON_SHOWN = ("doctor", "setup", "capabilities", "profile")


def _add_json_flag(p, name):
    """`--json` em todo subcomando (contrato §2): a saída já é JSON; não muda nada nesta versão."""
    shown = "Aceito; a saída já é JSON (reservado para o modo humano)"
    p.add_argument("--json", action="store_true", help=shown if name in _JSON_SHOWN else argparse.SUPPRESS)


def machine_output(args, environ=None):
    """A saída tem que ser JSON de máquina? `--json` ou `GB_OUTPUT=json` (sem contar espaços em volta, como o aviso
    do `config`); outro valor não muda nada."""
    environ = os.environ if environ is None else environ
    return bool(getattr(args, "json", False)) or environ.get("GB_OUTPUT", "").strip() == "json"


def _add_toolchain_subcommands(sub):
    """Acrescenta os subcomandos sem `--project` obrigatório.

    São eles: providers, doctor, plugins, x, setup, capabilities, profile, client, template, recipe e environment.
    """
    names = (
        "providers",
        "doctor",
        "plugins",
        "x",
        "setup",
        "capabilities",
        "profile",
        "client",
        "template",
        "recipe",
        "environment",
    )
    for name in names:
        p = sub.add_parser(name, help=SUMMARIES[name], description=SUMMARIES[name])
        _SUBPARSERS[name] = p
        _add_json_flag(p, name)
        if name == "setup":
            mode = p.add_mutually_exclusive_group()
            mode.add_argument("--check", action="store_true", help="Só conferir, sem instalar nada")
            mode.add_argument(
                "--upgrade",
                choices=["ytdlp"],
                help="Atualizar o yt-dlp do runtime gerenciado além da versão fixada",
            )
            mode.add_argument(
                "--where",
                nargs="?",
                const="all",
                choices=["all", "venv", "tools"],
                help="Mostrar onde fica cada parte do runtime (JSON), sem instalar; sem valor, todas",
            )
        if name == "doctor":
            # O SKILL.md diz que `--project` vai em todo comando, e a primeira chamada
            # do fluxo é o `doctor`: recusá-lo ali é contradizer a instrução logo na
            # largada. Aceito e ignorado — o diagnóstico é da instalação, não do projeto.
            p.add_argument(
                "--project",
                help="Aceito por uniformidade e ignorado: o diagnóstico é da instalação, não do projeto",
            )
            p.set_defaults(project_not_operated=True)
            p.add_argument(
                "--live",
                action="store_true",
                help="Testar buscas reais/refresh; pode consumir quota de API",
            )
        if name == "plugins":
            p.add_argument(
                "--action",
                required=True,
                choices=[
                    "list",
                    "enable",
                    "disable",
                    "check",
                    "install",
                    "update",
                    "remove",
                    "new",
                    "marketplace-add",
                    "marketplace-list",
                    "marketplace-remove",
                    "marketplace-update",
                    "search",
                ],
                help=(
                    "list: inventário sem executar código; enable/disable: liga/desliga por id; check: valida uma "
                    "pasta; install/update: traz de pasta ou git, em dois passos; remove: tira a pasta e o estado, "
                    "em dois passos; new: gera um plugin mínimo; marketplace-add/-list/-remove/-update: índices de "
                    "plugins fixados por commit; search: procura nos índices em cache, sem rede"
                ),
            )
            p.add_argument(
                "--id",
                help="Id do plugin (enable/disable/update/remove/new); install: <id>@<marketplace> instala pelo índice",
            )
            p.add_argument(
                "--source",
                help=(
                    "Pasta local ou URL git (https:// ou git@) do plugin a instalar (install) ou do repositório do "
                    "marketplace (marketplace-add)"
                ),
            )
            p.add_argument(
                "--marketplace",
                help=("Nome do marketplace (marketplace-remove; marketplace-update e search, que sem ele usam todos)"),
            )
            p.add_argument("--query", help="search: trecho do id, da descrição ou do tipo de extensão a procurar")
            p.add_argument(
                "--all",
                action="store_true",
                help=(
                    "update: prévia das atualizações de todos os plugins instalados por marketplace, com o comando "
                    "de cada um (só prévia; confirme por id)"
                ),
            )
            p.add_argument(
                "--commit",
                help=(
                    "install/update: sha completo (40 hex) do commit a instalar; sem ele, a ref é resolvida na "
                    "origem (update com --commit volta a um commit anterior); marketplace-add/-update: commit do "
                    "índice"
                ),
            )
            p.add_argument(
                "--ref",
                help=(
                    "install/marketplace-add: branch, tag ou refs/... a resolver na origem (padrão HEAD); o update "
                    "resolve a mesma"
                ),
            )
            p.add_argument("--subdir", help="install: pasta do plugin dentro do repositório git (ex.: plugins/demo)")
            p.add_argument(
                "--allow-rollback",
                action="store_true",
                help=(
                    "marketplace-update: aceita fixar um commit do índice que não descende do fixado hoje "
                    "(volta atrás ou história reescrita)"
                ),
            )
            p.add_argument(
                "--path",
                help="check: pasta do plugin a validar (executa o register()); new: pasta onde criar o plugin",
            )
            p.add_argument(
                "--kind",
                choices=list(SCAFFOLD_KINDS),
                help="Tipo do plugin gerado por new: fonte, fonte com rota de download, comando ou exportador",
            )
            p.add_argument(
                "--yes",
                action="store_true",
                help=(
                    "Confirma enable/install/update/remove depois de mostrar manifesto, permissões e origem (ou o "
                    "que sai) à pessoa"
                ),
            )
            p.add_argument(
                "--expect",
                help=(
                    "sha256 mostrado na prévia (sem --yes) de install/update e do enable de um plugin cujo "
                    "conteúdo mudou desde o pin (suspenso ou desligado); obrigatório junto com --yes, "
                    "para confirmar que o conteúdo não mudou desde a prévia"
                ),
            )
        adder = _TOOLCHAIN_ARGUMENTS.get(name)
        if adder is not None:
            adder(p)
        if name == "x":
            p.add_argument("plugin_id", nargs="?", metavar="plugin", help="Id do plugin dono do comando")
            p.add_argument("plugin_command", nargs="?", metavar="comando", help="Nome do comando do plugin")
            p.add_argument("--list", action="store_true", help="Listar os comandos dos plugins habilitados, sem rodar")
            p.add_argument("--project", help="Pasta do projeto que o comando pode ler (somente leitura)")
            p.set_defaults(project_not_operated=True)
            p.add_argument(
                "--arg",
                action="append",
                metavar="CHAVE=VALOR",
                help="Argumento do comando; repita a flag para passar vários",
            )


PROFILE_ACTIONS = ("show", "trust", "untrust")


def _profile_renamed(args):
    """`profile <ação> [arquivo]`, a forma posicional da versão de testes: o `Rename` dela ou `None`."""
    # `arg`, nunca `token`: o ruff (S105) lê `token == "--action"` como senha escrita no código.
    if any(arg == "--action" or arg.startswith("--action=") for arg in args):
        return None
    word = next((arg for arg in args if arg in PROFILE_ACTIONS), None)
    if word is None:
        return None
    return Rename(
        f"profile {word}",
        f"profile --action {word}",
        "profile_positional",
        tail=" O getbrolls.toml vai em `--path`.",
        suggestion=f"--action {word}",
    )


def _add_profile_args(p):
    """Flags de `profile --action show|trust|untrust`."""
    p.add_argument(
        "--action",
        required=True,
        choices=list(PROFILE_ACTIONS),
        help="show: valor e origem de cada campo; trust: confiar (prévia, depois --yes --expect); untrust: desfazer",
    )
    p.add_argument("--path", help="Caminho do getbrolls.toml (padrão: o descoberto)")
    p.add_argument("--project", help="Pasta de onde a busca pelo getbrolls.toml começa (antes da pasta atual)")
    # `--project` só dá o ponto de partida da busca: o comando não opera sobre o projeto nem lê o seu layout.
    p.set_defaults(project_not_operated=True)
    p.add_argument("--yes", action="store_true", help="trust: confirma depois de mostrar a prévia à pessoa")
    p.add_argument("--expect", help="trust: sha256 mostrado na prévia; obrigatório junto com --yes")
    # A forma posicional da versão de testes (`profile show [arquivo]`) recusa com o comando novo.
    p.add_argument("legacy", nargs="*", help=argparse.SUPPRESS)
    p.renamed = _profile_renamed


# Comandos de projeto em que um grupo de flags (os `dest` do argparse) substitui `--project`:
# `init --client C --slug S` cria o projeto em `<pasta do cliente>/projetos/<slug>/`.
PROJECT_ALTERNATIVES: dict[str, tuple[tuple[str, ...], ...]] = {"init": (("client", "slug"),)}
_PROJECT_HELP = "Pasta do projeto que guarda brolls/, fora da instalação da skill"


# As flags próprias de cada comando de `_add_toolchain_subcommands` que tem `--action`.
_TOOLCHAIN_ARGUMENTS = {
    "profile": _add_profile_args,
    "client": _add_client_arguments,
    "template": _add_template_arguments,
    "recipe": _add_recipe_arguments,
    "environment": _add_environment_arguments,
}


def build_parser():
    """Monta o parser: opções globais e um subparser por subcomando, com as flags específicas de cada um."""
    parser = GbArgumentParser(
        prog="getbrolls",
        description="getbrolls — pesquisar, revisar e coletar trechos por fonte.",
        epilog="Use `<subcomando> --help` para os argumentos de cada etapa.",
    )
    parser.add_argument(
        "--env-file",
        help="Arquivo .env explícito; sem ele: GB_ENV_FILE, depois o .env do checkout, depois $GB_HOME/.env",
    )
    parser.add_argument(
        "--profile",
        help=(
            "getbrolls.toml explícito, ou `off` para não usar perfil; sem ela: GB_PROFILE, depois "
            "getbrolls.toml a partir de --project e da pasta atual"
        ),
    )
    parser.add_argument(
        "--version",
        action=_VersionAction,
        help="Mostrar a versão instalada, o Python e a origem dos dados (wheel ou checkout) e sair",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_toolchain_subcommands(sub)
    for name in (
        "status",
        "search",
        "resolve",
        "inspect",
        "preview",
        "approve",
        "permit",
        "reject",
        "fetch",
        "verify",
        "review",
        "import-review",
        "init",
        "migrate",
        "init-rules",
        "rules",
        "init-brief",
        "brief",
        "remember",
        "references",
        "learn",
        "library",
        "browser-plan",
        "queue",
        "serve",
        "deliver",
        "roteiro",
        "assets",
        "export",
        "analysis",
    ):
        p = sub.add_parser(name, help=SUMMARIES[name], description=SUMMARIES[name])
        _SUBPARSERS[name] = p
        _add_json_flag(p, name)
        alternatives = PROJECT_ALTERNATIVES.get(name, ())
        p.add_argument(
            "--project",
            required=not alternatives,
            help=_PROJECT_HELP
            if not alternatives
            else "Pasta do projeto novo (ou --client e --slug: <pasta do cliente>/projetos/<slug>/)",
        )
        _add_project_subcommand_args(p, name)

    return parser


def _add_confirm_format_change_arg(p, name):
    """`--confirm-format-change`, visível só nos comandos que chegam a `sync_formats`."""
    # `roteiro`, `assets`, `export` e `analysis` nascem sem a flag: eles nunca chegam a `sync_formats`.
    if name in ("status", "init", "migrate", "roteiro", "assets", "export", "analysis"):
        return
    # Mudar o formato-alvo derruba aprovações humanas; qualquer comando que
    # sincronize formato precisa deste sim explícito antes de apagá-las. Mas
    # `execute()` só chega a `sync_formats` (commands.py) depois de passar
    # pelos retornos antecipados de serve/queue/init-rules/init-brief/brief/
    # learn/library/rules e por cima de READ_ONLY_CONSULTS (references,
    # inspect) — nesses a flag continua aceita (scripts e agentes já a
    # passam para eles) mas some do `--help` porque nunca teve efeito ali.
    reaches_sync_formats = name in FORMAT_GATE_SUBCOMMANDS
    p.add_argument(
        "--confirm-format-change",
        action="store_true",
        help="Confirmar que aprovações já dadas podem ser invalidadas pela mudança de formato"
        if reaches_sync_formats
        else argparse.SUPPRESS,
    )


def _add_roteiro_args(p, name):
    """Flags de `roteiro` (new/check/review/plan/sync)."""
    if name != "roteiro":
        return
    from .roteiro import GENRES

    p.add_argument(
        "--action",
        required=True,
        choices=["new", "check", "review", "plan", "sync"],
        help=(
            "new: esqueleto; check: valida e mostra o plano de cena; review: registra a revisão humana; "
            "plan: mostra o sync sem gravar; sync: grava ids e beats"
        ),
    )
    p.add_argument("--genero", choices=sorted(GENRES), help="Gênero do conteúdo (new)")
    p.add_argument("--tema", help="Tema do vídeo numa linha (new)")
    p.add_argument(
        "--force",
        action="store_true",
        help="Recomeçar do esqueleto; o atual vira ROTEIRO.md.bak, ou uma cópia com data se ele já existe (new)",
    )
    p.add_argument("--by", help="Nome de quem revisou o roteiro (review)")
    p.add_argument("--channel", choices=["chat"], default="chat", help="Por onde a revisão chegou (review)")
    p.add_argument("--statement", help="Frase exata dita por quem revisou (review)")
    p.add_argument(
        "--expect",
        help=(
            "review.sha256 que check e plan mostram, da versão que a pessoa viu; obrigatório no review, "
            "que recusa se o roteiro mudou desde então (review)"
        ),
    )
    p.add_argument(
        "--confirm-target-change",
        action="store_true",
        help="Aceitar que aprovações de beats com alvo novo voltem a pendente (sync)",
    )


def _add_assets_args(p, name):
    """Flags de `assets` (list/where)."""
    if name != "assets":
        return
    from .assets import ASSET_KINDS

    p.add_argument(
        "--action",
        required=True,
        choices=["list", "where"],
        help="list: inventário; where: onde um nome resolve",
    )
    p.add_argument("--kind", choices=sorted(ASSET_KINDS), help="Tipo de componente")
    p.add_argument("--name", help="Nome do componente, sem extensão (where)")


def _add_analysis_args(p, name):
    """Flags de `analysis` (list/check/register)."""
    if name != "analysis":
        return
    from .vocab import MEDIA_ROLES

    p.add_argument(
        "--action",
        required=True,
        choices=["list", "check", "register"],
        help=(
            "list: mídias registradas; check: confere os arquivos de analysis/; register: calcula o id por "
            "conteúdo e grava media.json (list e check só leem)"
        ),
    )
    p.add_argument("--path", help="Mídia a registrar, relativa ao projeto, com / (ex.: aroll/c01.mp4) (register)")
    p.add_argument(
        "--role",
        choices=list(MEDIA_ROLES),
        help="Papel da mídia (register); sem ele, vem da pasta: aroll/, broll/, assets/musica/, assets/sfx/",
    )


def _add_export_args(p, name):
    """Flags de `export`."""
    if name != "export":
        return
    p.add_argument(
        "--to", required=True, help="Exporter de destino, de um plugin de terceiros habilitado (ex.: meu_exporter)"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostrar a pasta, os arquivos e a mídia que o export gravaria, sem gravar nada",
    )


def _add_serve_args(p, name):
    """Flags de `serve`."""
    if name != "serve":
        return
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--background",
        action="store_true",
        help="Subir o servidor num processo solto e devolver a URL na hora (PID em brolls/.serve.pid)",
    )
    g.add_argument(
        "--stop",
        action="store_true",
        help="Encerrar o servidor de fundo pelo PID gravado em brolls/.serve.pid",
    )
    p.add_argument(
        "--port",
        type=_IntOption(_port),
        default=None,
        help="Porta local para o servidor (padrão 8767; se ocupada, usa uma porta livre)",
    )


def _add_deliver_args(p, name):
    """Flags de `deliver`."""
    if name != "deliver":
        return
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostrar o que iria para entrega/ sem criar, ligar ou apagar nada",
    )


def _add_queue_args(p, name):
    """Flags de `queue` (add/next/mark/status)."""
    if name != "queue":
        return
    mark_help = (
        "mark: item falhou; motivo com 403/429, challenge/login, 'rate limit'/'too many requests' ou as "
        "mensagens de bloqueio da própria skill (sessão de acesso, IP bloqueado, limite de requisições) "
        "abre cooldown"
    )
    p.add_argument(
        "--action",
        choices=["add", "next", "mark", "status"],
        required=True,
        help=(
            "add: enfileirar URLs; next: próximo item ou tempo de espera; mark: registrar resultado; "
            "status: contagens e cooldown"
        ),
    )
    p.add_argument(
        "--provider",
        choices=["instagram", "tiktok", "youtube"],
        help="Fonte das URLs em add; em next, limita a fila a essa fonte",
    )
    p.add_argument("urls", nargs="*", help="URLs públicas a enfileirar (add); repetidas são ignoradas")
    p.add_argument("--url", action="append", help="URL pública a enfileirar (add); pode repetir")
    p.add_argument("--id", help="ID do item retornado por next (mark)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--done", action="store_true", help="mark: item coletado com sucesso; zera o cooldown")
    g.add_argument("--failed", action="store_true", help=mark_help)
    g.add_argument("--skipped", action="store_true", help="mark: item pulado sem tentar")
    p.add_argument("--reason", help="Motivo real registrado no item (mark)")


def _add_candidate_arg(p, name):
    """`--candidate`, com a exigência e a repetição corretas para cada comando."""
    if name == "approve":
        # Repetível de propósito: "aprovei todos" do usuário quer dizer "os que
        # você me mostrou", e só quem mostrou sabe quais foram. Listar os IDs é
        # mais barato que descobrir depois que `--all` pegou um descarte com
        # prévia esquecida em disco.
        p.add_argument(
            "--candidate",
            action="append",
            help="ID do candidato a aprovar; repita a flag para aprovar vários (ou use --all)",
        )
    elif name == "reject":
        # Repetível como `approve`, e pelo mesmo motivo: quem descarta descarta em
        # leva, olhando a mesma lista que mostrou. Sem `--all`: rejeitar em massa o
        # que ninguém viu apagaria candidato bom por engano, e aqui não há `--all`
        # que valha o risco.
        p.add_argument(
            "--candidate",
            action="append",
            required=True,
            help="ID do candidato a rejeitar; repita a flag para rejeitar vários",
        )
    elif name in ("preview", "permit", "fetch", "remember"):
        p.add_argument(
            "--candidate",
            required=True,
            help="ID do candidato retornado por search/resolve",
        )


def _add_fetch_args(p, name):
    """Flags de `fetch`."""
    if name != "fetch":
        return
    p.add_argument(
        "--reacquire",
        action="store_true",
        help=(
            "Rota de plugin cuja licença já foi consumida e cujo arquivo sumiu do cache: "
            "roda a rota de novo (nova licença/cota), só com o ok da pessoa"
        ),
    )


def _add_start_end_args(p, name):
    """`--start`/`--end`, comuns a `preview` e `approve`."""
    if name not in ("preview", "approve"):
        return
    p.add_argument("--start", type=float, help="Início do trecho na origem, em segundos")
    p.add_argument("--end", type=float, help="Fim do trecho na origem, em segundos")


def _add_inspect_args(p, name):
    """Flags de `inspect`."""
    if name != "inspect":
        return
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--candidate",
        help="ID do candidato já registrado; grava só media.duration_s",
    )
    g.add_argument("--url", help="URL pública da fonte, sem registrar candidato")
    p.add_argument(
        "--query",
        help="Fala ou alvo do trecho; pontua as janelas candidatas",
    )
    p.add_argument(
        "--max-windows",
        type=int,
        default=3,
        help="Quantas janelas candidatas devolver, 1–20 (padrão 3)",
    )


def _add_preview_args(p, name):
    """Flags de `preview` além de `--candidate`/`--start`/`--end`."""
    if name != "preview":
        return
    p.add_argument(
        "--scan",
        action="store_true",
        help="Varrer o vídeo inteiro num contact sheet de baixa resolução, sem definir intervalo",
    )
    p.add_argument(
        "--reference-only",
        action="store_true",
        help="Gerar apenas referência estática, sem obter trecho remoto",
    )
    p.add_argument("--narration", help="Fala exata do roteiro")
    p.add_argument("--reason", help="Decisão de coleta desta fonte")


def _add_import_review_args(p, name):
    """Flags de `import-review`."""
    if name != "import-review":
        return
    p.add_argument(
        "--file",
        help="JSON de decisões; sem esta flag usa o mais recente de brolls/reviews/",
    )
    p.add_argument("--by", required=True, help="Nome de quem revisou e assinou as decisões")


def _add_approve_args(p, name):
    """Flags de `approve` além de `--candidate`/`--start`/`--end`."""
    if name != "approve":
        return
    p.add_argument(
        "--by",
        required=True,
        help="Nome de quem já aprovou explicitamente o trecho",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=(
            "Aplicar a mesma aprovação a todo candidato com prévia gerada e sem "
            "aprovação válida; use só quando todos eles foram mostrados à pessoa"
        ),
    )
    p.add_argument(
        "--channel",
        choices=["chat", "storyboard"],
        default="chat",
        help="Por onde a decisão humana chegou; padrão chat",
    )
    p.add_argument(
        "--statement",
        help="Frase exata dita por quem aprovou, registrada literalmente",
    )


def _add_permit_args(p, name):
    """Flags de `permit`."""
    if name != "permit":
        return
    p.add_argument(
        "--preset",
        # Sem `choices=`: isso exigiria hashear a pasta de todo plugin instalado
        # (`presets.names()` -> `loader.declared`) a cada comando, `--help`
        # incluso, e ignoraria o `GB_PLUGINS`/`GB_HOME` de um `--env-file` (lido
        # depois do parser). `main()` valida logo depois do `.env`, antes de
        # abrir o projeto (`_check_preset_name`), com a mensagem do argparse.
        help=(
            "Condições genéricas da fonte, sempre com o pedido de conferir a "
            "página original. Nomes embutidos: " + ", ".join(sorted(presets.PERMIT_PRESETS)) + "; "
            "ou o nome de um preset de plugin habilitado."
        ),
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--evidence", help="Evidência real fornecida ou verificada")
    g.add_argument(
        "--declaration",
        action="store_true",
        help="Registrar declaração que o usuário preencheu em RULES.md",
    )
    p.add_argument(
        "--declared-by",
        help="Nome de quem declarou a responsabilidade pelo uso, dito no chat",
    )
    p.add_argument(
        "--declaration-text",
        help="Frase literal da declaração de responsabilidade, com 20 caracteres ou mais",
    )


def _add_remember_args(p, name):
    """Flags de `remember`."""
    if name != "remember":
        return
    p.add_argument(
        "--decision",
        choices=["approved", "rejected"],
        required=True,
        help="Decisão humana registrada para esta referência",
    )
    p.add_argument("--reason", required=True, help="Motivo real da decisão registrada")
    p.add_argument("--by", required=True, help="Nome de quem decidiu")


def _add_learn_args(p, name):
    """Flags de `learn`."""
    if name != "learn":
        return
    p.add_argument("--query", help="Busca real que você fez, como digitada na fonte")
    p.add_argument("--provider", help="Fonte onde essa busca rodou (exige --query)")
    p.add_argument(
        "--outcome",
        choices=["hit", "miss"],
        help="hit: a busca rendeu material usável; miss: não rendeu (exige --query)",
    )
    p.add_argument("--preference", help="Preferência editorial dita pela pessoa, literal")
    p.add_argument(
        "--from-candidate",
        help="ID do candidato já memorizado com `remember`, guardado como ponteiro",
    )
    p.add_argument("--shot", help="Beat em que esse trecho foi usado")
    p.add_argument("--note", help="Observação livre, gravada em notes/<sha>.md")
    p.add_argument("--by", help="Nome de quem disse a preferência")


def _add_library_args(p, name):
    """Flags de `library`."""
    if name != "library":
        return
    p.add_argument(
        "--search",
        required=True,
        help="Termo procurado entre assets, buscas e preferências guardadas",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Máximo de resultados por tipo, 1–20 (padrão 5)",
    )


def _checked(parse):
    """Tipo de argparse a partir de um parser que levanta `ValueError`: valor ruim é erro de uso."""

    def convert(text):
        try:
            return parse(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    convert.__name__ = parse.__name__
    return convert


class _IntOption:  # pylint: disable=too-few-public-methods
    """Tipo de argparse de um inteiro validado por `parse` (como `_checked`).

    `value_type` faz o `capabilities` continuar descrevendo a opção como `int`, como uma
    opção de `type=int`."""

    value_type = "int"

    def __init__(self, parse):
        self._convert = _checked(parse)
        self.__name__ = parse.__name__

    def __call__(self, text):
        return self._convert(text)


PORT_RANGE = (1, 65535)


def _port(text):
    """Porta TCP de `--port`: 1 a 65535. Fora disso o bind daria `OverflowError` (saída 3), e
    0 viraria a porta padrão em silêncio."""
    low, high = PORT_RANGE
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"porta inválida: {text!r}; use um número de {low} a {high}.") from None
    if not low <= value <= high:
        raise ValueError(f"porta {value} fora do intervalo; use um número de {low} a {high}.")
    return value


def _project_slug(text):
    from .layout import check_client

    return check_client(text, "--slug")


def _client_slug(text):
    from .layout import check_client

    return check_client(text, "--client")


def _parse_canvas(text):
    from .layout import parse_canvas

    return parse_canvas(text)


def _parse_fps(text):
    from .layout import parse_fps

    return parse_fps(text)


def _init_renamed(args):
    """`init --template` (também abreviado a partir de `--templ`): o `Rename` dele, antes de qualquer outro erro."""
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag.startswith("--templ") and "--template".startswith(flag):
            return renamed.init_template()
    return None


def _add_init_args(p, name):
    """Flags de `init`."""
    if name != "init":
        return
    p.add_argument(
        "--client",
        type=_checked(_client_slug),
        help="Slug do cliente registrado do projeto (ex.: cliente-exemplo); obrigatório com --recipe",
    )
    p.add_argument(
        "--slug",
        type=_checked(_project_slug),
        help=(
            "Slug do projeto novo; com --client, ele nasce em <pasta do cliente>/projetos/<slug>/ "
            "(no lugar de --project)"
        ),
    )
    p.add_argument(
        "--recipe",
        type=_checked(_recipe_ref),
        help=(
            "Criar o projeto da receita do cliente --client: <slug>[@N], <cliente>.<slug>[@N] ou "
            "cat:getbrolls/recipe/<cliente>.<slug>@<N> (sem @N, a última versão)"
        ),
    )
    # `--template` da versão de testes: recusado com o `--recipe` (saída 2), nunca lido.
    p.add_argument("--template", dest="old_template", help=argparse.SUPPRESS)
    p.renamed = _init_renamed
    p.add_argument("--tema", help="Tema do ROTEIRO.md criado da receita (obrigatório com --recipe)")
    p.add_argument(
        "--canvas", type=_checked(_parse_canvas), help="Tamanho da tela em pixels, LARGURAxALTURA (ex.: 1080x1920)"
    )
    p.add_argument("--fps", type=_checked(_parse_fps), help="Quadros por segundo: inteiro (30) ou fração (30000/1001)")


def _add_migrate_args(p, name):
    """Flags de `migrate`."""
    if name != "migrate":
        return
    p.add_argument(
        "--action",
        required=True,
        choices=["plan", "apply"],
        help="plan: mostra o project.json que seria gravado, sem gravar; apply: grava",
    )
    p.add_argument(
        "--client",
        type=_checked(_client_slug),
        help="Slug do cliente (padrão: o cliente do frontmatter do ROTEIRO.md, se houver)",
    )


def _add_init_rules_args(p, name):
    """Flags de `init-rules`."""
    if name != "init-rules":
        return
    p.add_argument(
        "--mode",
        choices=["per_item_evidence", "user_declaration"],
        help="Modo de direitos gravado no bloco JSON; padrão per_item_evidence",
    )
    p.add_argument(
        "--responsible",
        help="Nome de quem assume a responsabilidade no modo user_declaration",
    )
    p.add_argument(
        "--declaration",
        help="Texto literal da declaração de responsabilidade do usuário",
    )
    p.add_argument(
        "--format",
        dest="video_format",
        choices=list(vocab.FORMATS),
        help="Formato-alvo gravado em video_format; regravar exige --force",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Regravar o RULES.md existente com as escolhas informadas",
    )


def _add_brief_args(p, name):
    """Flags de `brief`."""
    if name != "brief":
        return
    p.add_argument(
        "--validate",
        action="store_true",
        help="Só conferir o BRIEF.md e dizer o que está errado, sem listar comandos",
    )
    p.add_argument(
        "--beat",
        help="Mostrar apenas este beat, pelo id gravado no BRIEF.md",
    )


def _add_browser_plan_args(p, name):
    """Flags de `browser-plan`."""
    if name != "browser-plan":
        return
    p.add_argument("--url", required=True, help="URL pública da página a capturar")


def _add_search_args(p, name):
    """Flags de `search`."""
    if name != "search":
        return
    p.add_argument(
        "--provider",
        default="auto",
        help=(
            "Fonte: youtube, pexels, pixabay, commons, nasa, uma fonte de plugin habilitado ou auto "
            "(padrão); rode `providers` para listar as disponíveis, inclusive as de plugin"
        ),
    )
    p.add_argument("--query", required=True, help="Termos da busca na fonte")
    p.add_argument("--limit", type=int, default=8, help="Máximo de candidatos, 1–50 (padrão 8)")
    p.add_argument(
        "--intent",
        choices=["literal", "illustrative"],
        default="literal",
        help="literal: entidade nomeada; illustrative: ideia genérica",
    )
    p.add_argument(
        "--media",
        choices=["image", "video", "any"],
        default="any",
        help="Tipo de arquivo na fonte: image, video ou any (padrão); só NASA e Commons têm os dois",
    )
    p.add_argument(
        "--shot",
        help="Beat do BRIEF.md a que estes candidatos pertencem, ex.: abertura",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Listar o que a fonte devolveu sem registrar nada no projeto",
    )


def _add_reject_args(p, name):
    """Flags de `reject` além de `--candidate`."""
    if name != "reject":
        return
    p.add_argument(
        "--reason",
        help="Por que este material foi descartado; fica gravado no candidato",
    )


def _add_resolve_args(p, name):
    """Flags de `resolve`."""
    if name != "resolve":
        return
    p.add_argument("--context-image", help="Print opcional da pessoa; permanece estático")
    p.add_argument(
        "--full-preview-file",
        help="Composição pronta contendo apenas este insert, usada em GB_GIF_SCOPE=full",
    )
    p.add_argument(
        "--asset-type",
        choices=["video", "image", "news_screenshot", "web_screenshot"],
        help="Tipo do arquivo local; padrão é inferido pela extensão",
    )
    p.add_argument("--title", help="Título do asset/notícia")
    p.add_argument("--captured-at", help="Data da captura, ISO 8601")
    p.add_argument("--source-url", help="URL pública original do arquivo local")
    p.add_argument("--creator", help="Autor informado da fonte")
    p.add_argument("--shot", help="Identificador único do insert, ex.: insert-02")
    p.add_argument(
        "--intent",
        choices=["literal", "illustrative"],
        default="literal",
        help="literal: entidade nomeada; illustrative: ideia genérica",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--url",
        help="URL pública da fonte (YouTube, Instagram, TikTok, Wikimedia Commons, NASA)",
    )
    g.add_argument("--file", help="Arquivo local já autorizado para importação")


_PROJECT_SUBCOMMAND_ARG_ADDERS = (
    _add_confirm_format_change_arg,
    _add_roteiro_args,
    _add_assets_args,
    _add_analysis_args,
    _add_export_args,
    _add_serve_args,
    _add_deliver_args,
    _add_queue_args,
    _add_candidate_arg,
    _add_fetch_args,
    _add_start_end_args,
    _add_inspect_args,
    _add_preview_args,
    _add_import_review_args,
    _add_approve_args,
    _add_permit_args,
    _add_remember_args,
    _add_learn_args,
    _add_library_args,
    _add_init_args,
    _add_migrate_args,
    _add_init_rules_args,
    _add_brief_args,
    _add_browser_plan_args,
    _add_search_args,
    _add_reject_args,
    _add_resolve_args,
)


def _add_project_subcommand_args(p, name):
    """Acrescenta as flags específicas de `name` ao subparser (todo comando com `--project`)."""
    for add_args in _PROJECT_SUBCOMMAND_ARG_ADDERS:
        add_args(p, name)


def _check_project_alternatives(args):
    """`--project` ou um grupo completo de `PROJECT_ALTERNATIVES`, nunca nenhum; `--slug` nunca com `--project`."""
    groups = PROJECT_ALTERNATIVES.get(args.command, ())
    if not groups:
        return
    # Cada subparser é um `GbArgumentParser` (o `add_subparsers` herda a classe): o `error` aceita `suggestion`.
    parser = cast("GbArgumentParser", _SUBPARSERS[args.command])
    project = getattr(args, "project", None)
    if project == "":
        parser.error("argumento --project: não pode ser vazio (diga a pasta do projeto)", suggestion="--project")
    # lenient: dynamic getattr over argparse flag names, not an existence check on a path
    if project is None and not any(all(getattr(args, dest, None) for dest in group) for group in groups):
        named = " ou ".join(" e ".join(f"--{dest.replace('_', '-')}" for dest in group) for group in groups)
        parser.error(f"faltam argumentos obrigatórios: --project (ou {named})", suggestion="--project")
    if project is not None and getattr(args, "slug", None):
        parser.error("argumento --slug: não vale junto com --project (com --project, a pasta já é o projeto)")


def parse_args(argv=None):
    args = build_parser().parse_args(argv)
    _check_project_alternatives(args)
    return args


def _given_option_names(argv, args):
    """Names of the options passed, never their values (values can be URLs or free text).

    A token only counts when the parsed namespace has that option: a free-text value
    that happens to start with `--` must not reach the log as if it were a flag name.
    """
    names = []
    for token in argv:
        if not token.startswith("--"):
            continue
        name = token[2:].split("=", 1)[0]
        if hasattr(args, name.replace("-", "_")) and name not in names:
            names.append(name)
    return ",".join(names) if names else None


def _log_early_refusal(args, error_code, reason):
    """Registra `command_refused` quando o `.env` ou o perfil param o comando antes do log.

    Esse erro sai antes do log, da trava e da auditoria: nenhum arquivo nasce, nem o log do app.
    Só o espelho do stderr (`GB_LOG_STDERR=1`) mostra a linha, antes do JSON de erro. `reason` é
    o motivo do perfil (`untrusted`, `changed`, `invalid` ou `requires`, ou o de um arquivo do
    `GB_HOME` do perfil, como `link`) ou o do `.env` (`env_missing`, `env_process_only_key`,
    `env_gb_home`, `env_unreadable`, `env_encoding`); um erro de uso sem motivo próprio sai como
    `usage`, e um erro de E/S que não é o `.env` (pasta sem permissão no caminho), como `io`.
    """
    logs.configure(None, read_only=True)
    logs.event(
        logs.get("cli"),
        logging.WARNING,
        "command_refused",
        command=args.command,
        error_code=error_code,
        reason=reason,
    )
    logs.shutdown()


def _load_env_early(args):
    """Ativa o perfil e carrega o `.env` antes de configurar o log (GB_LOG_LEVEL pode morar nele).

    Erro de uso (`UsageError`: arquivo que falta, `GB_ENV_FILE` ou `GB_HOME` onde não
    podem, `getbrolls.toml` inválido ou não confiável) vira `OperationError` aqui mesmo,
    sem tocar no projeto; `requires` do perfil que não bate vira `PREREQUISITE_MISSING`, e um
    `.env` que não abre (`EnvReadError`) ou outra falha de E/S (`OSError`: pasta sem
    permissão no caminho do perfil ou do `$GB_HOME`), `IO_ERROR`.
    Outro erro do `.env` (chave desconhecida) fica para `execute()`, que o levanta dentro
    da auditoria; o carregamento repetido lá é inofensivo (`setdefault`, e a ativação do
    perfil é reaproveitada).
    """
    from .config import load_environment

    try:
        load_environment(args, warn=False)
    except UsageError as exc:
        _log_early_refusal(args, "USAGE_ERROR", exc.reason or "usage")
        raise OperationError(
            {"operation": args.command, "status": "error", "error_code": "USAGE_ERROR", "message": str(exc)}
        ) from None
    except PrerequisiteError as exc:
        _log_early_refusal(args, "PREREQUISITE_MISSING", "requires")
        raise OperationError(
            {"operation": args.command, "status": "error", "error_code": "PREREQUISITE_MISSING", "message": str(exc)}
        ) from None
    except ValueError:
        pass  # `execute()` levanta de novo, dentro da auditoria
    except OSError as exc:
        # Erro de E/S da pessoa (permissão), não bug: sai como os outros erros do `.env`, antes
        # do log e sem tocar no projeto. `env_unreadable` só quando é o próprio `.env`.
        _log_early_refusal(args, "IO_ERROR", "env_unreadable" if isinstance(exc, EnvReadError) else "io")
        raise OperationError(
            {"operation": args.command, "status": "error", "error_code": "IO_ERROR", "message": io_error_message(exc)}
        ) from None


def io_error_message(exc):
    """Mensagem em português de um `OSError` de antes do comando: o `.env` que não abre, ou o
    caminho (arquivo ou pasta) que não deu para acessar."""
    detail = exc.strerror or str(exc)
    if isinstance(exc, EnvReadError):
        shown = getattr(exc, "shown", None) or labels.shown(exc.filename)
        return f"Não consegui ler {shown} ({detail}). Confira as permissões do arquivo e repita."
    return labels.os_message(exc)


def _expand_project(args):
    """`--project '~/…'` com o `~` cru (entre aspas, `--project=~/…`, PowerShell) vale a pasta pessoal.

    Uma vez só, logo depois do parse: trava, manifesto, regras e log leem o mesmo caminho.
    Sem isso, `status` e `brief` expandiam por conta própria e o resto gravava em `./~/…`."""
    project = getattr(args, "project", None)
    labels.forget_typed_project()
    if project:
        # As digitado (`~/x` inclusive): as recusas devolvem isto, nunca a pasta pessoal expandida.
        args.project_typed = project
        try:
            expanded = str(Path(project).expanduser())
            if _unknown_tilde_user(project):
                raise RuntimeError(project)
            args.project = expanded
            labels.note_typed_project(project, expanded)
            _refuse_link_loop(project, expanded)
        except RuntimeError:
            # `~outro_usuario/…` de uma conta que não existe aqui (ou sem pasta pessoal): é o
            # caminho digitado que não serve, não um bug, e nenhum comando deve rodar com ele.
            raise UsageError(
                f"--project {project}: não consegui expandir o `~` (usuário desconhecido ou sem pasta "
                "pessoal). Use o caminho completo da pasta do projeto."
            ) from None


def _is_link_loop(exc):
    """Windows has no `ELOOP`: a loop is `ERROR_CANT_RESOLVE_FILENAME`, with `errno` 22."""
    return exc.errno == errno.ELOOP or getattr(exc, "winerror", None) == _paths.WINERROR_CANT_RESOLVE_FILENAME


def _refuse_link_loop(typed, path):
    """`--project` cujo caminho passa por um laço de links: erro de uso, igual no 3.11 (`RuntimeError` do
    `resolve`) e no 3.14 (`ELOOP`). Qualquer outro erro do `stat` fica para o comando.

    O Windows só diz "laço" quando o laço é o último nome; com o laço num componente do meio o
    erro do caminho inteiro é outro (caminho não encontrado). Aí os pais são perguntados do mais
    próximo ao mais longe, até o primeiro que responde sem erro: um laço em qualquer um deles
    é o laço do caminho."""
    path = Path(path)
    try:
        path.stat()
    except OSError as exc:
        if _is_link_loop(exc) or _parent_loops(path):
            raise UsageError(
                f"--project {typed}: o caminho tem um laço de links simbólicos ou um link que não resolve. "
                "Aponte para a pasta de verdade do projeto."
            ) from None


def _parent_loops(path):
    """Algum pai de `path`, antes do primeiro que responde ao `stat` sem erro, é um laço de links?"""
    for parent in path.parents:
        try:
            parent.stat()
        # Any other error on a parent (EACCES, ENOENT, ENOTDIR) is walked past: the command refuses the project.
        except OSError as exc:
            if _is_link_loop(exc):
                return True
        else:
            return False
    return False


def _unknown_tilde_user(project):
    """`~nome/…` cuja pasta pessoal não existe nesta máquina?

    No POSIX um usuário desconhecido já faz o `expanduser` levantar; no Windows o `ntpath`
    adivinha `C:\\Users\\nome` sem conferir, e o comando rodaria numa pasta que não é de ninguém.
    `~` sozinho e `~nome` com pasta pessoal de verdade seguem expandidos como sempre."""
    if not project.startswith("~"):
        return False
    separators = "".join(sep for sep in (os.sep, os.altsep) if sep)
    head = project[1:]
    for separator in separators:
        head = head.split(separator, 1)[0]
    if not head:
        return False
    profile = Path("~" + head).expanduser()  # RuntimeError (POSIX, unknown user): the caller refuses it
    # lenient: a profile folder that cannot be inspected counts as missing (the refusal names the path)
    return not profile.is_dir()


def _expanded(path):
    """`path` com o `~` do começo expandido; sem pasta pessoal para resolver, como veio."""
    try:
        return str(Path(path).expanduser())
    except RuntimeError:
        return path


def _serve_writes(args):
    """O `serve` vai gravar no projeto? Então as linhas `event=op` dele vão para o getbrolls.log.

    `serve` não toma a trava nem cria a árvore (é somente leitura para a trava), mas o
    servidor grava `.serve.pid`, `.serve.log` e as decisões em `reviews/`, e o `--stop`
    mata um processo: o log do projeto registra isso. Só com a árvore pronta (o Storyboard
    já gerado; para o `--stop`, um `.serve.pid`), para nunca criar `brolls/` nem um log num
    `--stop` que não tem o que parar. Nunca levanta.
    """
    project = getattr(args, "project", None)
    if args.command != "serve" or not project:
        return False
    name = ".serve.pid" if getattr(args, "stop", False) else "review.html"
    try:
        return _paths.strict_is_file(Path(project).expanduser() / "brolls" / name)
    except (OSError, RuntimeError, ValueError):
        return False


def _serve_error_payload(exc):
    """O JSON de erro do `serve` em primeiro plano: `error`, `error_code` e, numa recusa com motivo, `reason_code`."""
    # `OSError` (o `.env` que não abre, a porta que o sistema nega) é IO_ERROR, saída 1.
    message = io_error_message(exc) if isinstance(exc, OSError) else str(exc)
    payload = {"error": message, "error_code": error_code_for(exc)}
    if isinstance(exc, Refusal):
        payload["reason_code"] = exc.reason_code
    return payload


def main(argv=None):
    """Faz o parse, configura logging/trava e roda o comando com auditoria e log de início/fim."""
    from .commands import execute, with_summary

    args = parse_args(argv)
    _expand_project(args)
    logs.set_command(args.command)
    if args.command == "serve" and not (args.background or args.stop):
        # `serve` blocks in serve_forever() and owns its own stdout contract (one JSON
        # line with the URLs, printed by serve.run() itself, then nothing else): it does
        # not go through the JSON-wrapping in entrypoint(), so it exits directly here.
        try:
            from . import serve  # local: only this command needs the server

            serve.filter_output()  # the background child: its own log gets redacted lines only
            # O `.env` e o perfil antes do log: `GB_LOG_LEVEL=off` pode morar neles. Um erro aqui
            # sai pelo mesmo JSON de erro de `execute()`, logo abaixo.
            from .config import load_environment

            load_environment(args, warn=False)
            if _serve_writes(args):
                logs.configure(args.project, read_only=False)
            raise SystemExit(execute(args))
        except (ValueError, OSError) as exc:
            # `OSError` (o `.env` que não abre, a porta que o sistema nega) é IO_ERROR, saída 1.
            payload = _serve_error_payload(exc)
            print(json.dumps(payload, ensure_ascii=False))
            raise SystemExit(exit_code_for(payload["error_code"])) from None
        finally:
            logs.shutdown()

    # `.env` que falta ou que tenta definir o que não pode é erro de uso: sai antes do
    # log, da trava e da auditoria, sem criar nada em `brolls/`. (`serve` em primeiro
    # plano, acima, tem contrato próprio de saída e recebe o erro de `execute()`.)
    _load_env_early(args)
    project = getattr(args, "project", None)
    read_only = args.command in READ_ONLY_COMMANDS or (args.command, getattr(args, "action", None)) in READ_ONLY_ACTIONS
    _check_preset_name(args)
    # `init`/`migrate` validam antes de criar: o log do app não pode criar `brolls/` antes disso.
    # Comando de etapa numa pasta que não existe é recusado: o log também não cria a árvore.
    tree_pending = missing_project_tree(args.command, project) or (
        args.command in SELF_LOCKED_COMMANDS and not (project and is_folder(Path(project, "brolls")))
    )
    # Sem projeto, quem muda a instalação grava `$GB_HOME/logs/getbrolls.log`; quem só lê não cria arquivo.
    logs.configure(
        project,
        read_only=(read_only or tree_pending) and not _serve_writes(args),
        installation=writes_installation(args),
    )

    try:
        log = logs.get("cli")
        logs.event(
            log,
            logging.INFO,
            "command_start",
            command=args.command,
            read_only=read_only,
            options=_given_option_names(sys.argv[1:] if argv is None else argv, args),
        )
        started = time.monotonic()
        try:
            result = audited(args, lambda parsed: with_summary(parsed.command, execute(parsed)))
        except OperationError as exc:
            # `reason` só quando a recusa trouxe um `reason_code`: o log sem ele fica como era.
            reason = {"reason": exc.payload["reason_code"]} if "reason_code" in exc.payload else {}
            logs.event(
                log,
                logging.INFO,
                "command_end",
                command=args.command,
                status="error",
                error_code=exc.payload.get("error_code"),
                **reason,
                ms=round((time.monotonic() - started) * 1000),
                outcome="error",
            )
            raise
        logs.event(
            log,
            logging.INFO,
            "command_end",
            command=args.command,
            status="ok",
            error_code=None,
            ms=round((time.monotonic() - started) * 1000),
            outcome="ok",
        )
        _RESULT_EXIT.set(result_exit(args.command, result, getattr(args, "action", None)))
        return result
    finally:
        logs.shutdown()


def result_exit(command, result, action=None):
    """Código de saída de um comando que deu certo: 4 para `doctor`/`setup` com
    `"ready": false`; 1 para `analysis --action check` com `"ok": false` e para
    `setup --upgrade` cuja atualização falhou com a venv refeita na versão fixada
    (`upgrade.status: "failed"`, `ready: true`) — o resultado sai em stdout do mesmo jeito,
    para portões por código de saída; 0 para o resto."""
    if command in PREREQUISITE_COMMANDS and isinstance(result, dict) and result.get("ready") is False:
        return EXIT_PREREQUISITE
    if (command, action) == ("analysis", "check") and isinstance(result, dict) and result.get("ok") is False:
        return EXIT_OPERATION_ERROR
    if command == "setup" and isinstance(result, dict) and (result.get("upgrade") or {}).get("status") == "failed":
        return EXIT_OPERATION_ERROR
    return EXIT_OK


def _print_error(payload):
    """Uma linha JSON de erro em stderr, sem traceback nem repr (esses ficam no diagnostics)."""
    clean = {k: v for k, v in payload.items() if k not in ("traceback", "repr")}
    print(json.dumps(clean, ensure_ascii=False), file=sys.stderr)


def _closed_pipe(exc):
    """O leitor do pipe fechou antes do fim da escrita em stdout (`| head`)?

    No POSIX isso é `BrokenPipeError` (EPIPE). No Windows a escrita num pipe sem leitor
    falha com ERROR_NO_DATA, que o CRT entrega como `OSError` EINVAL — só vale para a
    escrita do resultado, nunca para um EINVAL vindo do próprio comando.
    """
    return isinstance(exc, BrokenPipeError) or exc.errno in (errno.EPIPE, errno.EINVAL)


def _silence_stdout():
    """Depois de um `BrokenPipeError`, aponta stdout para o devnull: o flush da saída do
    interpretador não levanta de novo (nem imprime "Exception ignored ...")."""
    try:
        target = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return  # stdout sem descritor (redirecionado em memória): nada a flushar no fim
    if not isinstance(target, int):
        return
    # effect-exempt: opens os.devnull to silence a broken stdout pipe; nothing is written to any file
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, target)
    except OSError:
        pass
    finally:
        os.close(devnull)


def entrypoint():
    for stream in (sys.stdout, sys.stderr):
        # TextIO não declara `reconfigure`; quem não tiver cai no except.
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    _RESULT_EXIT.set(EXIT_OK)
    try:
        result = main()
        try:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            # Flush aqui, dentro do `try`: com `| head` o erro de pipe chega agora, e não no
            # encerramento do interpretador, onde viraria traceback em stderr.
            sys.stdout.flush()
        except OSError as exc:
            if not _closed_pipe(exc):
                raise
            _silence_stdout()
        return _RESULT_EXIT.get()
    except OperationError as exc:
        _print_error({"error": str(exc), **exc.payload})
        return exit_code_for(exc.payload.get("error_code"))
    except (UsageError, PrerequisiteError) as exc:
        # Levantado fora de `audited` (antes do parse terminar, por exemplo).
        code = error_code_for(exc)
        _print_error({"error": str(exc), "error_code": code})
        return exit_code_for(code)
    except KeyboardInterrupt:
        _print_error({"error": "Operação interrompida.", "error_code": "INTERRUPTED"})
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        # The consumer end of a pipe (e.g. `| head`) closed early; this is an ordinary,
        # expected shutdown, not a bug — do not report it as INTERNAL_ERROR. The command
        # already ran: its exit (4 for `doctor` with `ready: false`) still stands.
        _silence_stdout()
        return _RESULT_EXIT.get()
    except Exception as exc:  # noqa: BLE001 - last-resort CLI boundary, must exit as JSON not a raw traceback
        return _internal_error(exc)


def _internal_error(exc):
    """Último recurso: grava o traceback no diagnostics e mostra só o tipo do erro."""
    # Anything audited() didn't already turn into an OperationError (e.g. an argparse-time
    # bug) must still exit as JSON, not a raw traceback breaking the CLI's output contract.
    from .runtime import REDACT_LIMIT, redact_diagnostic, redacted_traceback, write_diagnostics_log

    project = _project_from_argv()
    event = {
        "operation": None,
        "status": "error",
        "error_code": "INTERNAL_ERROR",
        "type": type(exc).__name__,
        "repr": labels.for_log(redact_diagnostic(repr(exc), limit=None), REDACT_LIMIT),
        "traceback": redacted_traceback(),
    }
    log = write_diagnostics_log(project, event)
    app_log = logs.log_path(project)  # nunca levanta: arquivo que não dá para conferir é `None`
    message = f"Erro interno inesperado (bug) [type: {type(exc).__name__}]."
    if log:
        message += f" Detalhes em {labels.shown(log)} (diagnostics.jsonl)."
    else:
        message += " Não foi possível gravar diagnostics.jsonl."
    _print_error(
        {
            "error": message,
            "error_code": "INTERNAL_ERROR",
            "type": type(exc).__name__,
            "log": str(log) if log else None,
            "app_log": str(app_log) if app_log else None,
        }
    )
    return EXIT_INTERNAL_ERROR


def _project_from_argv():
    """Best-effort --project value from sys.argv, for diagnostics logging before/around parse_args.

    Expanded like `_expand_project` does after the parse, so both name the same folder."""
    argv = sys.argv[1:]
    value = None
    if "--project" in argv:
        index = argv.index("--project")
        if index + 1 < len(argv):
            value = argv[index + 1]
    if value is None:
        value = next((item.split("=", 1)[1] for item in argv if item.startswith("--project=")), None)
    return _expanded(value) if value else None
