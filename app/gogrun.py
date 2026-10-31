"""Run gogcli (https://github.com/steipete/gogcli) for an agent, on one account, inside the owner's limits.

The broker hands gog a fresh access token through the environment; gog never sees a refresh token, a
client secret or this app's data folder. Each run gets an empty home and working folder that is deleted
afterwards. The owner's service switches become gog's own `--enable-commands`, so gog itself refuses
anything outside them, and the arguments are checked here first so an agent cannot widen them.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

GOG = os.getenv('GOG_BIN', '/usr/local/bin/gog')
TIMEOUT = 90
MAX_OUTPUT = 400_000
MAX_ARGS = 64
MAX_ARG = 20_000

# Global flags that would change whose token, which client or which commands gog uses.
REFUSED_FLAGS = ('--access-token', '--home', '--client', '--account', '-a', '--enable-commands', '--enable-commands-exact',
                 '--disable-commands', '--color', '-v', '--verbose',
                 # Set by Wizard from the owner's agent access level (agent_access.py); an agent cannot turn them off.
                 '--gmail-no-send', '--no-gmail-no-send', '--readonly', '--no-readonly')
# Command roots that manage gog itself, not Google data.
REFUSED_ROOTS = {'auth', 'config', 'login', 'logout', 'completion', 'agent', 'upgrade', 'update', 'version'}
# Free-text values (a mail body may well start with "/" or mention "../"); never file paths.
TEXT_FLAGS = {'--body', '--subject', '--query', '--q', '--text', '--description', '--summary', '--title', '--name',
              '--note', '--message', '--location', '--comment'}
ALIASES = {'yt': 'youtube', 'cal': 'calendar', 'contact': 'contacts', 'doc': 'docs', 'sheet': 'sheets', 'slide': 'slides', 'mail': 'gmail', 'email': 'gmail'}


class RunProblem(ValueError):
    def __init__(self, code, message):
        super().__init__(code)
        self.code, self.message = code, message


def root_of(args):
    for item in args:
        if not item.startswith('-'):
            return ALIASES.get(item, item)
    return None


def check_args(args, allowed_roots):
    """Plain validation; raises RunProblem with a message the agent can act on."""
    if isinstance(args, str):
        raise RunProblem('invalid_arguments', 'Pass the gog command as a list of words, e.g. ["gmail", "search", "is:unread", "--max", "10"].')
    if not isinstance(args, list) or not args or len(args) > MAX_ARGS:
        raise RunProblem('invalid_arguments', f'Pass 1 to {MAX_ARGS} command words.')
    previous = ''
    for item in args:
        if not isinstance(item, str) or len(item) > MAX_ARG or '\x00' in item:
            raise RunProblem('invalid_arguments', 'Every command word must be text.')
        name = item.split('=', 1)[0]
        if item.startswith('-') and (name in REFUSED_FLAGS or (name.startswith('-a') and not name.startswith('--') and len(name) > 1)):
            raise RunProblem('flag_refused', f'{name} is set by Wizard. Choose the account with the "account" field instead.')
        is_text = item.startswith('-') and name in TEXT_FLAGS
        value = item.split('=', 1)[1] if item.startswith('-') and '=' in item else item
        # Local files: gog runs in an empty folder; nothing else on this box is reachable.
        if not is_text and previous not in TEXT_FLAGS and (value.startswith(('/', '~')) or '../' in value or value == '..'):
            raise RunProblem('path_refused', 'Local paths are not available. Use names relative to the working folder, or read content with gog\'s own commands.')
        previous = item if item.startswith('-') and '=' not in item else ''
    if args[0].startswith('-'):
        raise RunProblem('invalid_arguments', 'Start with the command, e.g. ["gmail", "search", ...]; flags come after it.')
    root = ALIASES.get(args[0], args[0])
    if root in REFUSED_ROOTS:
        raise RunProblem('command_refused', f'"gog {root}" manages gog itself; Wizard does that for you.')
    if root not in allowed_roots:
        raise RunProblem('service_off', f'"{root}" is not switched on for this account. The owner can switch it on in the Google app.')
    return root


def run(args, token, allowed_roots, *, timeout=TIMEOUT, binary=None, extra=()):
    """Run one gog command. Returns {'exit_code', 'output' (parsed JSON or text), 'stderr', 'truncated'}."""
    root = check_args(args, allowed_roots)
    args = [root, *args[1:]]  # canonical root, so gog's own --enable-commands matches aliases too
    binary = binary or GOG
    if not os.path.exists(binary):
        raise RunProblem('gog_missing', 'gog is not installed in this app.')
    work = tempfile.mkdtemp(prefix='gog-')
    try:
        home = os.path.join(work, 'home')
        os.mkdir(home, 0o700)
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': home, 'GOG_HOME': home, 'GOG_ACCESS_TOKEN': token,
               'NO_COLOR': '1', 'TZ': os.getenv('TZ', 'UTC'), 'LANG': 'C.UTF-8'}
        command = [binary, '--no-input', '--color=never', '--wrap-untrusted',
                   '--enable-commands=' + ','.join(sorted(allowed_roots)), *extra]
        wants_plain = any(a in ('--plain', '-p') for a in args)
        if not wants_plain and not any(a in ('--json', '-j') for a in args):
            command.append('--json')
        command += args
        try:
            done = subprocess.run(command, cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RunProblem('timeout', f'gog did not finish within {timeout} seconds. Ask for less (e.g. --max).') from None
        out, err = done.stdout, done.stderr
        truncated = len(out) > MAX_OUTPUT
        text = out[:MAX_OUTPUT].decode('utf-8', 'replace')
        err_text = _scrub(err[-4000:].decode('utf-8', 'replace'), token)
        output = text
        if not wants_plain and not truncated:
            try:
                output = json.loads(text) if text.strip() else None
            except ValueError:
                output = text
        return {'command': root, 'exit_code': done.returncode, 'output': output if not isinstance(output, str) else _scrub(output, token),
                'stderr': err_text, 'truncated': truncated}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _scrub(text, token):
    if token and token in text:
        text = text.replace(token, '[token]')
    return re.sub(r'ya29\.[A-Za-z0-9._-]+', '[token]', text)


def help_for(path, allowed_roots, *, binary=None):
    """gog's own machine-readable contract for a command (or the list of commands the account allows)."""
    binary = binary or GOG
    words = path.split() if isinstance(path, str) else []
    if not words:
        return {'commands': sorted(allowed_roots), 'tip': 'Ask for one, e.g. "gmail search", for its flags and output.'}
    if any(not re.fullmatch(r'[a-z][a-z0-9-]{0,40}', w) for w in words) or len(words) > 5:
        raise RunProblem('invalid_arguments', 'Name a command like "gmail search".')
    root = ALIASES.get(words[0], words[0])
    if root not in allowed_roots:
        raise RunProblem('service_off', f'"{root}" is not switched on for any account you may use.')
    work = tempfile.mkdtemp(prefix='gog-')
    try:
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': work, 'GOG_HOME': work, 'NO_COLOR': '1'}
        done = subprocess.run([binary, 'schema', *words], cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        if done.returncode != 0:
            done = subprocess.run([binary, *words, '--help'], cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
            return {'help': done.stdout[:60_000].decode('utf-8', 'replace')}
        try:
            value = json.loads(done.stdout)
        except ValueError:
            return {'help': done.stdout[:60_000].decode('utf-8', 'replace')}
        raw = json.dumps(value)
        if len(raw) > 60_000:
            # Big parents (e.g. "gmail") list every subcommand; keep names and summaries only.
            return {'help': _brief(value), 'note': 'Shortened. Ask for a subcommand for its flags.'}
        return {'schema': value}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _brief(value, depth=0):
    if isinstance(value, dict):
        keep = {}
        for key in ('name', 'path', 'help', 'summary', 'usage', 'aliases'):
            if key in value:
                keep[key] = value[key]
        for key in ('commands', 'subcommands', 'children'):
            if isinstance(value.get(key), list) and depth < 4:
                keep[key] = [_brief(v, depth + 1) for v in value[key]]
        if 'command' in value and depth < 4:
            keep['command'] = _brief(value['command'], depth + 1)
        return keep
    return value
