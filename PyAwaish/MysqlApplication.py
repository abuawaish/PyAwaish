import os
import re
from time import perf_counter
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from typing import Any
from flask_mysqldb import MySQL
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# SQL parsing helpers
#
# Used to turn a write statement (DML or schema change) into a readable
# report: which object it touched, what the statement asked for, and — for
# schema changes — what actually changed on the table.
# ---------------------------------------------------------------------------

_IDENT = r"(?:`[^`]+`|[\w$]+)"
_QUALIFIED = rf"(?:{_IDENT}\s*\.\s*)?{_IDENT}"
_OBJECT_MODIFIERS = (
    r"(?:online\s+|offline\s+|ignore\s+|temporary\s+|unique\s+|fulltext\s+|spatial\s+"
    r"|or\s+replace\s+|aggregate\s+|definer\s*=\s*\S+\s+)*"
)
_OBJECT_TYPES = (
    r"(table|database|schema|view|index|trigger|procedure|function|event|user|role"
    r"|server|tablespace|sequence|logfile\s+group)"
)
_IF_CLAUSE = r"(?:if\s+not\s+exists\s+|if\s+exists\s+)?"

_DML_VERBS = frozenset({'insert', 'replace', 'update', 'delete', 'merge', 'load'})
_DDL_VERBS = frozenset({'alter', 'rename', 'create', 'drop', 'truncate'})
_DCL_VERBS = frozenset({'grant', 'revoke'})
_TCL_VERBS = frozenset({'commit', 'rollback', 'savepoint', 'start', 'begin'})

_VERB_LABELS = {
    'insert': 'Insert', 'replace': 'Replace', 'update': 'Update', 'delete': 'Delete',
    'merge': 'Merge', 'load': 'Load Data', 'alter': 'Alter', 'rename': 'Rename',
    'create': 'Create', 'drop': 'Drop', 'truncate': 'Truncate', 'call': 'Call',
    'set': 'Set', 'start': 'Start Transaction', 'begin': 'Begin', 'commit': 'Commit',
    'rollback': 'Rollback', 'savepoint': 'Savepoint', 'grant': 'Grant',
    'revoke': 'Revoke', 'flush': 'Flush', 'analyze': 'Analyze', 'optimize': 'Optimize',
    'repair': 'Repair', 'check': 'Check', 'lock': 'Lock', 'unlock': 'Unlock',
}

# Column attributes compared when diffing a table before/after a schema change.
_COLUMN_ATTRIBUTES = (
    ('type', 'type'),
    ('null', 'nullability'),
    ('key', 'key'),
    ('default', 'default'),
    ('extra', 'extra'),
    ('collation', 'collation'),
    ('comment', 'comment'),
)


def _as_text(value: Any) -> Any:
    """Decodes bytes coming back from the driver, leaves everything else alone."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode('utf-8', 'replace')
    return value


def _plural(count: int, singular: str, plural: str = "") -> str:
    """'1 row' / '3 rows'."""
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def _was_were(count: int) -> str:
    """Keeps 'n row(s) was/were ...' notes grammatical."""
    return 'was' if count == 1 else 'were'


def _strip_sql_comments(query: str) -> str:
    """
    Removes comments and collapses whitespace so the regex based parsers below
    see one clean line. Quoted text is preserved as-is. Parsing only — the
    original query is what actually gets executed.
    """
    out: list[str] = []
    quote = ''
    index = 0
    length = len(query)

    while index < length:
        char = query[index]

        if quote:
            out.append(char)
            if char == '\\' and quote != '`' and index + 1 < length:
                out.append(query[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ''
            index += 1
            continue

        if char in "'\"`":
            quote = char
            out.append(char)
            index += 1
            continue

        if char == '#' or (query[index:index + 2] == '--' and (index + 2 >= length or query[index + 2] in ' \t\r\n')):
            while index < length and query[index] not in '\r\n':
                index += 1
            out.append(' ')
            continue

        if query[index:index + 2] == '/*':
            end = query.find('*/', index + 2)
            index = length if end == -1 else end + 2
            out.append(' ')
            continue

        out.append(char)
        index += 1

    return ' '.join(''.join(out).split())


def _detect_verb(query: str) -> str:
    """
    The first SQL keyword of a statement, derived from the comment-stripped
    text so leading comments, tabs, newlines or a parenthesis can't misroute
    the statement. Returns '' when no keyword is found.
    """
    cleaned = _strip_sql_comments(query).lstrip('(').strip()
    match = re.match(r'([A-Za-z]+)', cleaned)
    return match.group(1).lower() if match else ''


def _split_top_level(text: str) -> list[str]:
    """Splits on commas that sit outside brackets and quotes — DECIMAL(10,2) stays intact."""
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    quote = ''
    index = 0

    while index < len(text):
        char = text[index]

        if quote:
            buffer.append(char)
            if char == '\\' and quote != '`' and index + 1 < len(text):
                buffer.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ''
            index += 1
            continue

        if char in "'\"`":
            quote = char
            buffer.append(char)
            index += 1
            continue

        if char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)

        if char == ',' and depth == 0:
            parts.append(''.join(buffer).strip())
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    tail = ''.join(buffer).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def _clean_identifier(raw: str) -> str:
    """`schema`.`table` -> schema.table"""
    if not raw:
        return ''
    pieces = [piece.strip().strip('`').strip() for piece in re.split(r"\s*\.\s*", raw.strip())]
    return '.'.join(piece for piece in pieces if piece)


def _quote_identifier(name: str) -> str:
    """
    Re-quotes a parsed identifier for use in SHOW statements. Returns an empty
    string for anything that does not look like a plain identifier, so the
    caller can skip the lookup rather than build a questionable query.
    """
    if not name:
        return ''
    parts = []
    for piece in name.split('.'):
        piece = piece.strip()
        if not piece or not re.fullmatch(r"[\w$][\w$ \-]*", piece):
            return ''
        parts.append('`' + piece.replace('`', '``') + '`')
    return '.'.join(parts)


def _parse_info_string(info: str) -> dict[str, int]:
    """
    'Rows matched: 3  Changed: 1  Warnings: 0' -> {'rows_matched': 3, 'changed': 1, 'warnings': 0}
    """
    stats: dict[str, int] = {}
    for label, value in re.findall(r"([A-Za-z][A-Za-z ]*?)\s*:\s*(-?\d+)", info or ''):
        stats[label.strip().lower().replace(' ', '_')] = int(value)
    return stats


def _friendly_mysql_error(exc: Exception, host: str, port: int) -> tuple:
    """
    Turns a raw driver exception into a (title, detail, hint) triple for the
    pretty error card on the config page.
    """
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if len(exc.args) >= 2 and isinstance(exc.args[0], int):
        raw = str(exc.args[1])  # MySQLdb: (code, message) — drop the code prefix
    else:
        raw = str(exc)

    known = {
        1045: ("Access denied",
               "The username or password is incorrect for this host.",
               "Double-check the credentials, or create the user: "
               "CREATE USER 'user'@'%' IDENTIFIED BY 'password';"),
        1049: ("Unknown database",
               "The database does not exist on this server.",
               "Create it first (CREATE DATABASE name;) or fix the spelling."),
        2003: ("Can't reach the MySQL server",
               f"No MySQL server is listening at {host}:{port}.",
               "Verify the host/port and that mysqld is running and reachable."),
        2002: ("Can't connect (socket)",
               "The local MySQL socket is missing or unreachable.",
               "Start the MySQL service, or use 127.0.0.1 instead of localhost."),
        2013: ("Lost connection during handshake",
               "The server dropped the connection while connecting.",
               "Check server load, max_connections and network stability."),
        1130: ("Host not allowed",
               "This host is not allowed to connect to the server.",
               "Grant access: GRANT ALL ON db.* TO 'user'@'your-host';"),
    }
    if code in known:
        return known[code]
    detail = raw if len(raw) <= 300 else raw[:300] + "…"
    return ("Connection failed",
            detail or "An unknown error occurred while connecting.",
            "Check the host, port, credentials and that the MySQL server is running.")


def _extract_target(cleaned: str, verb: str) -> tuple[str, str]:
    """Returns (object_type, name) for the object a statement acts on."""
    patterns: dict[str, list[tuple[str, str]]] = {
        'insert': [('table', rf"^insert\s+(?:low_priority\s+|delayed\s+|high_priority\s+|ignore\s+)*(?:into\s+)?({_QUALIFIED})")],
        'replace': [('table', rf"^replace\s+(?:low_priority\s+|delayed\s+)*(?:into\s+)?({_QUALIFIED})")],
        'update': [('table', rf"^update\s+(?:low_priority\s+|ignore\s+)*({_QUALIFIED})")],
        'delete': [('table', rf"\bfrom\s+({_QUALIFIED})")],
        'truncate': [('table', rf"^truncate\s+(?:table\s+)?({_QUALIFIED})")],
        'load': [('table', rf"\binto\s+table\s+({_QUALIFIED})")],
        'rename': [
            ('table', rf"^rename\s+tables?\s+({_QUALIFIED})"),
            ('user', r"^rename\s+user\s+(\S+)"),
        ],
        'call': [('procedure', rf"^call\s+({_QUALIFIED})")],
    }

    for object_type, pattern in patterns.get(verb, []):
        match = re.search(pattern, cleaned, re.I)
        if match:
            return object_type, _clean_identifier(match.group(1))

    if verb in ('alter', 'create', 'drop'):
        match = re.match(
            rf"^{verb}\s+{_OBJECT_MODIFIERS}{_OBJECT_TYPES}\s+{_IF_CLAUSE}({_QUALIFIED})",
            cleaned,
            re.I,
        )
        if match:
            object_type = ' '.join(match.group(1).lower().split())
            return object_type, _clean_identifier(match.group(2))

    return '', ''


def _parse_rename_pairs(cleaned: str) -> list[tuple[str, str]]:
    """RENAME TABLE a TO b, c TO d -> [(a, b), (c, d)]"""
    match = re.match(r"^rename\s+tables?\s+(.*)$", cleaned, re.I | re.S)
    if not match:
        return []

    pairs: list[tuple[str, str]] = []
    for clause in _split_top_level(match.group(1)):
        pair = re.match(rf"^({_QUALIFIED})\s+to\s+({_QUALIFIED})$", clause.strip().rstrip(';'), re.I)
        if pair:
            pairs.append((_clean_identifier(pair.group(1)), _clean_identifier(pair.group(2))))
    return pairs


def _describe_alter_action(clause: str) -> tuple[str, tuple[str, str] | None, str]:
    """
    Turns one ALTER TABLE clause into a sentence.

    Returns (description, column_rename, table_rename) where the rename values
    are only set when the clause renames something — the diff step uses them so
    a rename is not reported as a drop plus an add.
    """
    text = ' '.join(clause.split()).rstrip(';')
    rules_prefix = rf"^(?:add|drop|modify|change|alter|rename)\b"

    def spec_of(raw: str) -> str:
        return ' '.join(raw.split()).strip()

    match = re.match(rf"^rename\s+column\s+({_IDENT})\s+to\s+({_IDENT})$", text, re.I)
    if match:
        old, new = _clean_identifier(match.group(1)), _clean_identifier(match.group(2))
        return f"Renamed column '{old}' to '{new}'", (old, new), ''

    match = re.match(rf"^rename\s+(?:index|key)\s+({_IDENT})\s+to\s+({_IDENT})$", text, re.I)
    if match:
        return f"Renamed index '{_clean_identifier(match.group(1))}' to '{_clean_identifier(match.group(2))}'", None, ''

    match = re.match(rf"^rename\s+(?:to|as\s+)?\s*({_QUALIFIED})$", text, re.I)
    if match:
        new_table = _clean_identifier(match.group(1))
        return f"Renamed the table to '{new_table}'", None, new_table

    match = re.match(rf"^change\s+(?:column\s+)?(?:if\s+exists\s+)?({_IDENT})\s+({_IDENT})\s*(.*)$", text, re.I)
    if match:
        old, new, spec = _clean_identifier(match.group(1)), _clean_identifier(match.group(2)), spec_of(match.group(3))
        if old == new:
            return f"Redefined column '{old}'" + (f" as {spec}" if spec else ''), None, ''
        detail = f" and redefined it as {spec}" if spec else ''
        return f"Renamed column '{old}' to '{new}'{detail}", (old, new), ''

    match = re.match(rf"^modify\s+(?:column\s+)?(?:if\s+exists\s+)?({_IDENT})\s*(.*)$", text, re.I)
    if match:
        spec = spec_of(match.group(2))
        return f"Modified column '{_clean_identifier(match.group(1))}'" + (f" to {spec}" if spec else ''), None, ''

    match = re.match(r"^add\s+(?:constraint\s+(?:\S+\s+)?)?primary\s+key\s*(\(.*\))?", text, re.I)
    if match:
        return f"Added a PRIMARY KEY{' on ' + spec_of(match.group(1)) if match.group(1) else ''}", None, ''

    match = re.match(rf"^add\s+(?:constraint\s+(?:({_IDENT})\s+)?)?foreign\s+key\s*(?:{_IDENT}\s*)?(\(.*?\))\s*references\s+({_QUALIFIED})\s*(\(.*?\))?", text, re.I)
    if match:
        name = _clean_identifier(match.group(1) or '')
        label = f"'{name}' " if name else ''
        target = _clean_identifier(match.group(3))
        return f"Added FOREIGN KEY {label}{spec_of(match.group(2))} referencing '{target}'{spec_of(match.group(4) or '')}", None, ''

    match = re.match(rf"^add\s+(?:constraint\s+(?:({_IDENT})\s+)?)?unique\s+(?:index\s+|key\s+)?(?:({_IDENT})\s*)?(\(.*\))?", text, re.I)
    if match:
        name = _clean_identifier(match.group(1) or match.group(2) or '')
        label = f" '{name}'" if name else ''
        return f"Added a UNIQUE index{label}{' on ' + spec_of(match.group(3)) if match.group(3) else ''}", None, ''

    match = re.match(rf"^add\s+(?:constraint\s+(?:({_IDENT})\s+)?)?check\s*(\(.*\))?", text, re.I)
    if match:
        name = _clean_identifier(match.group(1) or '')
        return f"Added a CHECK constraint{f' {name}' if name else ''}", None, ''

    match = re.match(rf"^add\s+(fulltext|spatial)\s+(?:index\s+|key\s+)?(?:({_IDENT})\s*)?(\(.*\))?", text, re.I)
    if match:
        name = _clean_identifier(match.group(2) or '')
        label = f" '{name}'" if name else ''
        return f"Added a {match.group(1).upper()} index{label}{' on ' + spec_of(match.group(3)) if match.group(3) else ''}", None, ''

    match = re.match(rf"^add\s+(?:index|key)\s+(?:({_IDENT})\s*)?(\(.*\))?", text, re.I)
    if match:
        name = _clean_identifier(match.group(1) or '')
        label = f" '{name}'" if name else ''
        return f"Added an index{label}{' on ' + spec_of(match.group(2)) if match.group(2) else ''}", None, ''

    match = re.match(r"^add\s+(?:column\s+)?\((.*)\)$", text, re.I)
    if match:
        names = [_clean_identifier((part.split() or [''])[0]) for part in _split_top_level(match.group(1))]
        names = [name for name in names if name]
        if names:
            return f"Added {_plural(len(names), 'column')}: {', '.join(names)}", None, ''

    match = re.match(rf"^add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?({_IDENT})\s*(.*)$", text, re.I)
    if match:
        spec = spec_of(match.group(2))
        return f"Added column '{_clean_identifier(match.group(1))}'" + (f" as {spec}" if spec else ''), None, ''

    if re.match(r"^drop\s+primary\s+key$", text, re.I):
        return "Dropped the PRIMARY KEY", None, ''

    match = re.match(rf"^drop\s+foreign\s+key\s+(?:if\s+exists\s+)?({_IDENT})$", text, re.I)
    if match:
        return f"Dropped FOREIGN KEY '{_clean_identifier(match.group(1))}'", None, ''

    match = re.match(rf"^drop\s+(?:index|key)\s+(?:if\s+exists\s+)?({_IDENT})$", text, re.I)
    if match:
        return f"Dropped index '{_clean_identifier(match.group(1))}'", None, ''

    match = re.match(rf"^drop\s+(?:check|constraint)\s+(?:if\s+exists\s+)?({_IDENT})$", text, re.I)
    if match:
        return f"Dropped constraint '{_clean_identifier(match.group(1))}'", None, ''

    match = re.match(rf"^drop\s+(?:column\s+)?(?:if\s+exists\s+)?({_IDENT})$", text, re.I)
    if match:
        return f"Dropped column '{_clean_identifier(match.group(1))}'", None, ''

    match = re.match(rf"^alter\s+(?:column\s+)?({_IDENT})\s+set\s+default\s+(.*)$", text, re.I)
    if match:
        return f"Set the default of column '{_clean_identifier(match.group(1))}' to {spec_of(match.group(2))}", None, ''

    match = re.match(rf"^alter\s+(?:column\s+)?({_IDENT})\s+drop\s+default$", text, re.I)
    if match:
        return f"Dropped the default of column '{_clean_identifier(match.group(1))}'", None, ''

    match = re.match(rf"^alter\s+(?:index|key)\s+({_IDENT})\s+(visible|invisible)$", text, re.I)
    if match:
        return f"Made index '{_clean_identifier(match.group(1))}' {match.group(2).lower()}", None, ''

    match = re.match(r"^convert\s+to\s+character\s+set\s+(\S+)(?:\s+collate\s+(\S+))?", text, re.I)
    if match:
        collation = f" (collation {match.group(2)})" if match.group(2) else ''
        return f"Converted the table to character set {match.group(1)}{collation}", None, ''

    match = re.match(r"^(?:default\s+)?(?:character\s+set|charset)\s*=?\s*(\S+)", text, re.I)
    if match:
        return f"Set the default character set to {match.group(1)}", None, ''

    match = re.match(r"^(?:default\s+)?collate\s*=?\s*(\S+)", text, re.I)
    if match:
        return f"Set the default collation to {match.group(1)}", None, ''

    match = re.match(r"^engine\s*=?\s*(\S+)", text, re.I)
    if match:
        return f"Changed the storage engine to {match.group(1)}", None, ''

    match = re.match(r"^auto_increment\s*=?\s*(\d+)", text, re.I)
    if match:
        return f"Set AUTO_INCREMENT to {match.group(1)}", None, ''

    match = re.match(r"^comment\s*=?\s*(.*)$", text, re.I)
    if match:
        return f"Set the table comment to {spec_of(match.group(1))}", None, ''

    match = re.match(r"^row_format\s*=?\s*(\S+)", text, re.I)
    if match:
        return f"Set the row format to {match.group(1)}", None, ''

    match = re.match(r"^(algorithm|lock)\s*=?\s*(\S+)", text, re.I)
    if match:
        return f"Execution hint: {match.group(1).upper()}={match.group(2).upper()}", None, ''

    if re.match(r"^(enable|disable)\s+keys$", text, re.I):
        return f"{text.split()[0].capitalize()}d the table keys", None, ''

    if re.match(r"^order\s+by\s+", text, re.I):
        return f"Reordered the rows on disk ({spec_of(text[8:])})", None, ''

    if re.match(rules_prefix, text, re.I) or text:
        return text if len(text) <= 160 else text[:157] + '...', None, ''

    return '', None, ''


def _parse_alter_actions(cleaned: str) -> tuple[list[str], str, list[tuple[str, str]]]:
    """Breaks an ALTER TABLE into readable clauses, the new table name and column renames."""
    match = re.match(
        rf"^alter\s+(?:online\s+|offline\s+|ignore\s+)*table\s+(?:if\s+exists\s+)?{_QUALIFIED}\s+(.*)$",
        cleaned,
        re.I | re.S,
    )
    if not match:
        return [], '', []

    descriptions: list[str] = []
    renames: list[tuple[str, str]] = []
    renamed_table = ''

    for clause in _split_top_level(match.group(1)):
        description, column_rename, table_rename = _describe_alter_action(clause)
        if description:
            descriptions.append(description)
        if column_rename:
            renames.append(column_rename)
        if table_rename:
            renamed_table = table_rename

    return descriptions, renamed_table, renames


def _column_summary(spec: dict[str, Any]) -> str:
    """'varchar(50) NOT NULL DEFAULT 'x' AUTO_INCREMENT' — a one line column shape."""
    bits = [str(spec.get('type') or '')]
    bits.append('NULL' if str(spec.get('null')).upper() == 'YES' else 'NOT NULL')

    default = spec.get('default')
    if default is not None:
        bits.append(f"DEFAULT {default}")

    if spec.get('extra'):
        bits.append(str(spec['extra']).upper())

    key = str(spec.get('key') or '').upper()
    if key == 'PRI':
        bits.append('PRIMARY KEY')
    elif key == 'UNI':
        bits.append('UNIQUE')

    return ' '.join(bit for bit in bits if bit)


def _diff_snapshots(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    column_renames: list[tuple[str, str]],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """
    Compares two table snapshots. Returns the list of changes and a
    {column: change kind} map so the resulting structure can flag its own rows.
    """
    if not before or not after:
        return [], {}

    before_columns: dict[str, Any] = before.get('columns', {})
    after_columns: dict[str, Any] = after.get('columns', {})
    changes: list[dict[str, str]] = []
    status: dict[str, str] = {}

    renamed = {old: new for old, new in column_renames if old in before_columns and new in after_columns}

    for old, new in renamed.items():
        differences = [
            f"{label}: {before_columns[old].get(key) or '—'} → {after_columns[new].get(key) or '—'}"
            for key, label in _COLUMN_ATTRIBUTES
            if before_columns[old].get(key) != after_columns[new].get(key)
        ]
        changes.append({
            'kind': 'renamed',
            'label': f"Column '{old}' renamed to '{new}'",
            'detail': '; '.join(differences) if differences else _column_summary(after_columns[new]),
        })
        status[new] = 'renamed'

    for name, spec in after_columns.items():
        if name in before_columns or name in renamed.values():
            continue
        changes.append({'kind': 'added', 'label': f"Column '{name}' added", 'detail': _column_summary(spec)})
        status[name] = 'added'

    for name, spec in before_columns.items():
        if name in after_columns or name in renamed:
            continue
        changes.append({'kind': 'removed', 'label': f"Column '{name}' removed", 'detail': _column_summary(spec)})

    for name, spec in after_columns.items():
        if name not in before_columns:
            continue
        differences = [
            f"{label}: {before_columns[name].get(key) or '—'} → {spec.get(key) or '—'}"
            for key, label in _COLUMN_ATTRIBUTES
            if before_columns[name].get(key) != spec.get(key)
        ]
        if differences:
            changes.append({'kind': 'modified', 'label': f"Column '{name}' modified", 'detail': '; '.join(differences)})
            status[name] = 'modified'

    before_indexes: dict[str, Any] = before.get('indexes', {})
    after_indexes: dict[str, Any] = after.get('indexes', {})

    for name, spec in after_indexes.items():
        if name not in before_indexes:
            kind = 'UNIQUE index' if spec.get('unique') else 'Index'
            if name == 'PRIMARY':
                kind = 'PRIMARY KEY'
            changes.append({
                'kind': 'added',
                'label': f"{kind} '{name}' added",
                'detail': f"on ({', '.join(spec.get('columns', []))})",
            })

    for name, spec in before_indexes.items():
        if name not in after_indexes:
            kind = 'UNIQUE index' if spec.get('unique') else 'Index'
            if name == 'PRIMARY':
                kind = 'PRIMARY KEY'
            changes.append({
                'kind': 'removed',
                'label': f"{kind} '{name}' removed",
                'detail': f"was on ({', '.join(spec.get('columns', []))})",
            })
        elif after_indexes[name] != spec:
            changes.append({
                'kind': 'modified',
                'label': f"Index '{name}' changed",
                'detail': f"({', '.join(spec.get('columns', []))}) → ({', '.join(after_indexes[name].get('columns', []))})",
            })

    return changes, status


class MysqlApplication:
    """

    MySQL Application with Flask Integration
    ----------------------------------------

    A Flask-based web application for interacting with MySQL databases. Provides a user-friendly interface
    to configure database connections, execute SQL queries, and perform CRUD operations securely.

    Features:
    ---------

    - Dynamic MySQL connection configuration via web interface
    - Secure environment variable handling for credentials
    - RESTful API endpoints for database operations
    - Real-time query execution with JSON responses
    - Structured write feedback: headlines, stats, notes, schema diffs, warnings, timing
    - Error handling and user feedback with flash messages
    - Support for both raw SQL queries and structured operations

    Initialization Parameters:
    --------------------------

    - secret_key (str):

      - Path to .env file containing 'SECRET_KEY' and database credentials OR
      - Direct secret key string for Flask session encryption
      - If empty, uses default credentials with security warnings

    Routes:
    -------

    - **GET /:** Configuration page for MySQL connection setup
    - **POST /config_mysql:** Handles database configuration form submission
    - **GET /home:** Main interface for executing database operations
    - **POST /execute_query:** Endpoint for processing SQL queries and operations

    Key Methods:
    ------------

    - execute(debug_mode, port_number, host_address, use_ssl, ssl_cert, ssl_key):
      Starts the Flask server (HTTPS by default)

      - **debug_mode (bool):** Enable/disable debug mode (default: False)
      - **port_number (int):** Port to run application (default: 5001)
      - **host_address (str):** Network interface binding (default: 0.0.0.0)
      - **use_ssl (bool):** Serve over HTTPS (default: True); pass False for plain HTTP
      - **ssl_cert / ssl_key (str | None):** Paths to your own certificate and
        private key. When omitted, a persistent self-signed certificate is
        generated automatically on first run and stored in ``~/.pyawaish/ssl/``

    Supported Operations via /execute_query:
    ----------------------------------------

    - Raw SQL queries (SELECT, INSERT, UPDATE, DELETE, CALL, etc.)
    - Structured operations:

      - **insert:** Add records with table/columns/values
      - **delete:** Remove records with table/condition
      - **update:** Modify records with table/field/condition
      - **fetch_data:** Retrieve all data from table
      - **show_tables:** List all tables in database

    Security Features:
    ------------------

    - Environment variable isolation
    - Connection validation before configuration
    - Parameterized query execution (via structured operations)
    - Secure secret key handling with fallback warnings

    Dependencies:
    -------------

    - Flask web framework
    - flask-mysqldb for MySQL integration
    - python-dotenv for environment management

    Example Usage:
    ---------------

    >>> app = MysqlApplication(secret_key="your_secret_key_or_env_path")
    >>> app.execute(debug_mode=False, port_number=5000)   # opens https://localhost:5000
    >>> app.execute(use_ssl=False)                        # plain http:// instead

    """

    def __init__(self , secret_key: str  = "") -> None:
        self.__secret_key = secret_key.strip()
        self.__load_environment(self.__secret_key)  # Load environment or set secret key

        # Initialize Flask app
        self.__app = Flask(__name__)
        self.__app.secret_key = self.__secret_key
        self.__database_name = ""

        # Securely load database configuration
        self.__app.config['MYSQL_HOST'] = os.getenv('MYSQL_HOST', 'localhost')
        self.__app.config['MYSQL_USER'] = os.getenv('MYSQL_USER', 'root')
        self.__app.config['MYSQL_PASSWORD'] = os.getenv('MYSQL_PASSWORD', '')
        self.__app.config['MYSQL_DB'] = os.getenv('MYSQL_DB', '')
        self.__app.config['MYSQL_PORT'] = int(os.getenv('MYSQL_PORT', '3306') or 3306)

        # Initialize MySQL
        self.__mysql = MySQL(self.__app)

        # Define routes
        self.__add_config_routes()
        self.__add_home_routes()
        self.__add_config_mysql()
        self.__add_execute_query()

    def __load_environment(self, env_input: str) -> None:
        """
        Handles loading the .env file if the user provides a path or filename.
        If a random string is provided, it's used directly as a secret key.
        """
        self.__env_path = None  # Initialize as None

        if env_input == "":
            print(f"⚠️  Warning: No secret key is provided. Using default secret key.")
            self.__secret_key = "default_secret_key"
            return

        if env_input:  # If user provides input
            if os.path.isabs(env_input) or os.path.exists(env_input):
                self.__env_path = env_input  # Absolute path or existing relative path
            elif os.path.exists(os.path.join(os.getcwd(), env_input)):
                self.__env_path = os.path.join(os.getcwd(), env_input)  # File in current dir
            else:
                print(f"🔐  Using provided secret key: {env_input}")
                self.__secret_key = env_input
                return

        else:  # If no secret key is provided
            default_env_path = os.path.join(os.getcwd(), ".env")
            if os.path.exists(default_env_path):  # Only load if .env exists
                self.__env_path = default_env_path

        # Load environment variables if a valid file is found
        if self.__env_path and os.path.exists(self.__env_path):
            load_dotenv(self.__env_path)
            secret_key = os.getenv("SECRET_KEY")
            if secret_key:
                self.__secret_key = secret_key
                print(f"✅ Loaded environment variables from {self.__env_path}")
            else:
                print(f"⚠️  Failed to load SECRET_KEY from '{self.__env_path}' The app runs with the default secret key.")
                self.__secret_key = "default_secret_key"
        else:
            print(f"⚠️  Warning: No .env file found. Using default secret key.")
            self.__secret_key = "default_secret_key"


    def __add_config_routes(self) -> None:
        @self.__app.route('/')
        def config_mysql_page() -> Any:
            return render_template('config_mysql.html')

    def __add_home_routes(self) -> None:
        @self.__app.route('/home')
        def home() -> Any:
            return render_template('home.html')

    def __add_config_mysql(self) -> None:
        @self.__app.route('/config_mysql', methods=['POST'])
        def config_mysql() -> Response:
            host = (request.form.get('host') or '').strip()
            username = (request.form.get('username') or '').strip()
            password = request.form.get('password') or ''
            database = (request.form.get('database') or '').strip()
            port_raw = (request.form.get('port') or '3306').strip()

            try:
                port = int(port_raw)
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                flash(("Invalid port",
                       f"'{port_raw}' is not a valid port number.",
                       "Use a number between 1 and 65535 (MySQL default: 3306)."), 'danger')
                return redirect(url_for('config_mysql_page'))

            # Update app configuration dynamically
            self.__app.config['MYSQL_HOST'] = host
            self.__app.config['MYSQL_USER'] = username
            self.__app.config['MYSQL_PASSWORD'] = password
            self.__app.config['MYSQL_DB'] = database
            self.__app.config['MYSQL_PORT'] = port
            self.__database_name = database

            try:
                # Test the connection
                cursor = self.__mysql.connection.cursor()
                cursor.execute('SELECT 1')
                cursor.close()
                flash('Connection established successfully!', 'success')
                return redirect(url_for('home'))
            except Exception as e:
                flash(_friendly_mysql_error(e, host, port), 'danger')
                return redirect(url_for('config_mysql_page'))

    def __connection_info(self) -> str:
        """
        The driver's status line for the last statement, e.g.
        'Rows matched: 3  Changed: 1  Warnings: 0'. Not every build exposes it.
        """
        try:
            info = self.__mysql.connection.info()
            return _as_text(info) or ''
        except Exception:
            return ''

    def __fetch_warnings(self, cursor: Any) -> list[dict[str, Any]]:
        """
        Reads SHOW WARNINGS for the statement that just ran. Must be called
        before COMMIT or any other statement, both of which reset the list.
        """
        try:
            cursor.execute("SHOW WARNINGS")
            rows = cursor.fetchall() or []
        except Exception:
            return []

        warnings: list[dict[str, Any]] = []
        for row in rows[:25]:
            row = [_as_text(value) for value in row]
            if len(row) >= 3:
                warnings.append({'level': str(row[0]), 'code': row[1], 'message': str(row[2])})
            elif row:
                warnings.append({'level': 'Warning', 'code': '', 'message': str(row[-1])})
        return warnings

    def __snapshot_table(self, cursor: Any, table_name: str) -> dict[str, Any] | None:
        """
        Captures the columns and indexes of a table so a schema change can be
        reported as a real before/after diff. Returns None when the table
        cannot be inspected (missing, or no privilege) — the caller then falls
        back to describing the statement itself.
        """
        quoted = _quote_identifier(table_name)
        if not quoted:
            return None

        snapshot: dict[str, Any] = {'columns': {}, 'indexes': {}}

        try:
            cursor.execute(f"SHOW FULL COLUMNS FROM {quoted}")
            rows = cursor.fetchall() or []
            names = [description[0].lower() for description in (cursor.description or [])]
        except Exception:
            return None

        for row in rows:
            record = {name: _as_text(value) for name, value in zip(names, row)}
            field = record.get('field')
            if not field:
                continue
            snapshot['columns'][str(field)] = {
                'name': str(field),
                'type': record.get('type'),
                'null': record.get('null'),
                'key': record.get('key'),
                'default': record.get('default'),
                'extra': record.get('extra'),
                'collation': record.get('collation'),
                'comment': record.get('comment'),
            }

        try:
            cursor.execute(f"SHOW INDEX FROM {quoted}")
            index_rows = cursor.fetchall() or []
            index_names = [description[0].lower() for description in (cursor.description or [])]
        except Exception:
            return snapshot

        for row in index_rows:
            record = {name: _as_text(value) for name, value in zip(index_names, row)}
            key_name = record.get('key_name')
            if not key_name:
                continue
            entry = snapshot['indexes'].setdefault(
                str(key_name),
                {'unique': str(record.get('non_unique')) == '0', 'columns': []},
            )
            column_name = record.get('column_name')
            if column_name:
                entry['columns'].append(str(column_name))

        return snapshot

    def __table_structure(self, snapshot: dict[str, Any] | None, status: dict[str, str]) -> list[dict[str, Any]]:
        """Flattens a snapshot into rows for the structure table, tagged with what changed."""
        if not snapshot:
            return []
        return [
            {
                'column': spec.get('name'),
                'type': spec.get('type'),
                'null': spec.get('null'),
                'key': spec.get('key'),
                'default': spec.get('default'),
                'extra': spec.get('extra'),
                'status': status.get(str(spec.get('name')), ''),
            }
            for spec in snapshot.get('columns', {}).values()
        ]

    def __build_feedback(self, cursor: Any, query: str, verb: str, elapsed_ms: float,
                         rows_affected: int, last_row_id: int | None, info: str,
                         warnings: list[dict[str, Any]], before: dict[str, Any] | None,
                         snapshot_target: str) -> dict[str, Any]:
        """
        Assembles the report shown after a write. Every branch fills the same
        shape so the front end has one thing to render:

            severity  — success | notice   (notice = ran fine but changed nothing)
            headline  — the one line summary
            stats     — labelled counters
            notes     — clarifications worth surfacing
            changes   — schema diff entries
            structure — the resulting columns, for schema changes
        """
        cleaned = _strip_sql_comments(query)
        object_type, target = _extract_target(cleaned, verb)
        parsed = _parse_info_string(info)
        label = _VERB_LABELS.get(verb, verb.upper())
        quoted_target = f"'{target}'" if target else ('the table' if object_type == 'table' else 'the object')

        feedback: dict[str, Any] = {
            'severity': 'success',
            'category': ('DDL' if verb in _DDL_VERBS else
                         'DML' if verb in _DML_VERBS else
                         'DCL' if verb in _DCL_VERBS else
                         'TCL' if verb in _TCL_VERBS else 'SQL'),
            'statement': label,
            'verb': verb.upper(),
            'object_type': object_type,
            'target': target,
            'headline': '',
            'notes': [],
            'stats': [],
            'changes': [],
            'structure': [],
            'warnings': warnings,
            'duration_ms': round(elapsed_ms, 2),
        }

        stats: list[dict[str, Any]] = []
        notes: list[str] = []

        # ---- DML ---------------------------------------------------------
        if verb in ('insert', 'replace'):
            records = parsed.get('records', rows_affected)
            duplicates = parsed.get('duplicates', 0)
            upsert = bool(re.search(r"\bon\s+duplicate\s+key\s+update\b", cleaned, re.I))

            if verb == 'replace':
                inserted = records if records else rows_affected
                feedback['headline'] = f"{_plural(max(inserted, 0), 'row')} written to {quoted_target}"
                if rows_affected > inserted:
                    replaced = rows_affected - inserted
                    notes.append(
                        f"{_plural(replaced, 'existing row')} {_was_were(replaced)} replaced — "
                        "REPLACE deletes the conflicting row, then inserts the new one."
                    )
            elif upsert:
                # MySQL reports 1 per fresh insert and 2 per row updated in place.
                updated = max(0, rows_affected - records) if records else 0
                feedback['headline'] = f"{_plural(max(records, 0), 'row')} processed on {quoted_target}"
                if updated:
                    notes.append(f"{_plural(updated, 'existing row')} {_was_were(updated)} updated instead of inserted (ON DUPLICATE KEY UPDATE).")
            else:
                inserted = records if records else rows_affected
                feedback['headline'] = f"{_plural(max(inserted, 0), 'row')} inserted into {quoted_target}"

            stats.append({'label': 'Rows affected', 'value': rows_affected})
            if records and records != rows_affected:
                stats.append({'label': 'Records read', 'value': records})
            if duplicates:
                stats.append({'label': 'Duplicates', 'value': duplicates})
                notes.append(f"{_plural(duplicates, 'row')} {_was_were(duplicates)} not inserted as new — it collided with an existing key.")
            if last_row_id:
                stats.append({'label': 'Last insert ID', 'value': last_row_id})
                notes.append(f"AUTO_INCREMENT value assigned to the first inserted row: {last_row_id}.")
            if rows_affected == 0:
                feedback['severity'] = 'notice'
                notes.append("No rows were added — the statement ran without error but nothing changed.")

        elif verb == 'update':
            matched = parsed.get('rows_matched', rows_affected)
            changed = parsed.get('changed', rows_affected)

            feedback['headline'] = f"{_plural(max(changed, 0), 'row')} updated in {quoted_target}"
            stats.append({'label': 'Rows matched', 'value': matched})
            stats.append({'label': 'Rows changed', 'value': changed})

            if matched == 0:
                feedback['severity'] = 'notice'
                feedback['headline'] = f"No rows matched in {quoted_target}"
                notes.append("Nothing was updated — no row satisfied the WHERE condition. Check the condition and the values it compares.")
            elif changed == 0:
                feedback['severity'] = 'notice'
                feedback['headline'] = f"{_plural(matched, 'row')} matched, nothing changed in {quoted_target}"
                notes.append("The matched rows already held these values, so MySQL wrote nothing.")
            elif changed < matched:
                untouched = matched - changed
                subject = "it already held" if untouched == 1 else "they already held"
                notes.append(f"{_plural(untouched, 'matched row')} {_was_were(untouched)} left untouched — {subject} the new values.")
            if not re.search(r"\bwhere\b", cleaned, re.I):
                notes.append("This UPDATE had no WHERE clause, so it applied to every row in the table.")

        elif verb == 'delete':
            feedback['headline'] = f"{_plural(max(rows_affected, 0), 'row')} deleted from {quoted_target}"
            stats.append({'label': 'Rows deleted', 'value': rows_affected})
            if rows_affected == 0:
                feedback['severity'] = 'notice'
                feedback['headline'] = f"No rows deleted from {quoted_target}"
                notes.append("No row satisfied the WHERE condition, so nothing was removed.")
            elif not re.search(r"\bwhere\b", cleaned, re.I):
                notes.append("This DELETE had no WHERE clause — every row in the table was removed.")

        elif verb == 'load':
            feedback['headline'] = f"{_plural(max(rows_affected, 0), 'row')} loaded into {quoted_target}"
            for key, text in (('records', 'Records'), ('deleted', 'Deleted'), ('skipped', 'Skipped')):
                if key in parsed:
                    stats.append({'label': text, 'value': parsed[key]})

        # ---- Schema changes ---------------------------------------------
        elif verb == 'alter':
            actions, renamed_table, column_renames = _parse_alter_actions(cleaned)
            after = self.__snapshot_table(cursor, snapshot_target) if snapshot_target else None
            changes, status = _diff_snapshots(before, after, column_renames)

            if object_type == 'table':
                display = renamed_table or target
                feedback['headline'] = f"Table '{display}' altered"
                if renamed_table and target and renamed_table != target:
                    feedback['headline'] = f"Table '{target}' altered and renamed to '{renamed_table}'"
                    feedback['target'] = renamed_table
            else:
                feedback['headline'] = f"{(object_type or 'object').capitalize()} {quoted_target} altered"

            feedback['changes'] = changes
            feedback['structure'] = self.__table_structure(after, status)

            if actions:
                notes.extend(actions)
            if after and not changes and object_type == 'table':
                notes.append("The statement completed, but the resulting structure is identical to the previous one.")
                feedback['severity'] = 'notice'
            if before is not None and after is None and object_type == 'table':
                notes.append("The statement completed; the resulting structure could not be read back for comparison.")

            if after:
                stats.append({'label': 'Columns now', 'value': len(after.get('columns', {}))})
                stats.append({'label': 'Indexes now', 'value': len(after.get('indexes', {}))})
            if changes:
                stats.append({'label': 'Schema changes', 'value': len(changes)})
            if parsed.get('records'):
                stats.append({'label': 'Rows copied', 'value': parsed['records']})
                notes.append(f"MySQL rebuilt the table and copied {_plural(parsed['records'], 'row')}.")

        elif verb == 'rename':
            pairs = _parse_rename_pairs(cleaned)
            after = None

            if pairs:
                feedback['target'] = pairs[0][1]
                if len(pairs) == 1:
                    old, new = pairs[0]
                    feedback['headline'] = f"Table '{old}' renamed to '{new}'"
                else:
                    feedback['headline'] = f"{_plural(len(pairs), 'table')} renamed"
                for old, new in pairs:
                    feedback['changes'].append({
                        'kind': 'renamed',
                        'label': f"'{old}' → '{new}'",
                        'detail': 'Table renamed',
                    })
                stats.append({'label': 'Tables renamed', 'value': len(pairs)})

                if len(pairs) == 1:
                    after = self.__snapshot_table(cursor, pairs[0][1])
                    if after:
                        feedback['structure'] = self.__table_structure(after, {})
                        stats.append({'label': 'Columns', 'value': len(after.get('columns', {}))})
                        notes.append(f"The data and structure are unchanged — only the name moved to '{pairs[0][1]}'.")
                    else:
                        notes.append("The rename completed; the new table could not be read back for confirmation.")
            else:
                feedback['headline'] = f"{label} completed"

        elif verb == 'truncate':
            feedback['headline'] = f"Table {quoted_target} truncated"
            notes.append("Every row was removed and AUTO_INCREMENT was reset. TRUNCATE cannot be rolled back and does not report a row count.")
            after = self.__snapshot_table(cursor, target)
            if after:
                feedback['structure'] = self.__table_structure(after, {})
                stats.append({'label': 'Columns', 'value': len(after.get('columns', {}))})
                notes.append("The table structure is intact — only the data was cleared.")

        elif verb in ('create', 'drop'):
            noun = (object_type or 'object').replace('_', ' ')
            action = 'created' if verb == 'create' else 'dropped'
            feedback['headline'] = f"{noun.capitalize()} {quoted_target} {action}"

            if verb == 'create' and object_type == 'table' and target:
                after = self.__snapshot_table(cursor, target)
                if after:
                    feedback['structure'] = self.__table_structure(after, {})
                    stats.append({'label': 'Columns', 'value': len(after.get('columns', {}))})
                    stats.append({'label': 'Indexes', 'value': len(after.get('indexes', {}))})

            if re.search(r"\bif\s+not\s+exists\b", cleaned, re.I) and verb == 'create':
                notes.append("IF NOT EXISTS was used — if the object already existed, nothing was created.")
            if re.search(r"\bif\s+exists\b", cleaned, re.I) and verb == 'drop':
                notes.append("IF EXISTS was used — if the object did not exist, nothing was dropped.")

        elif verb == 'call':
            feedback['headline'] = f"Procedure {quoted_target} executed"
            if rows_affected > 0:
                stats.append({'label': 'Rows affected', 'value': rows_affected})
                notes.append(f"The procedure reported {_plural(rows_affected, 'affected row')}.")
            else:
                notes.append("The procedure completed and returned no result set.")

        # ---- Privileges ---------------------------------------------------
        elif verb in ('grant', 'revoke'):
            action = 'granted' if verb == 'grant' else 'revoked'
            feedback['headline'] = f"Privileges {action} successfully"
            notes.append("The privilege change takes effect immediately for new connections.")

        # ---- Transactions -------------------------------------------------
        elif verb in ('commit', 'rollback', 'savepoint', 'start', 'begin'):
            headlines = {
                'commit': 'Transaction committed',
                'rollback': 'Transaction rolled back',
                'savepoint': 'Savepoint created',
                'start': 'Transaction started',
                'begin': 'Transaction started',
            }
            feedback['headline'] = headlines[verb]
            if verb in ('start', 'begin'):
                notes.append("Subsequent statements run inside this transaction until COMMIT or ROLLBACK.")
            elif verb == 'rollback':
                notes.append("All changes made since the transaction began were discarded.")

        else:
            feedback['headline'] = f"{label} statement executed successfully"
            if rows_affected > 0:
                stats.append({'label': 'Rows affected', 'value': rows_affected})

        if warnings:
            stats.append({'label': 'Warnings', 'value': len(warnings)})
        stats.append({'label': 'Duration', 'value': f"{round(elapsed_ms, 1)} ms"})

        feedback['stats'] = stats
        feedback['notes'] = notes
        return feedback

    def __run_write_statement(self, cursor: Any, query: str, verb: str) -> dict[str, Any]:
        """
        Runs one write statement and reports on it.

        Ordering matters: the row count, status line and warnings all describe
        the *last* statement on the connection, so they are captured before the
        COMMIT and before any snapshot query.
        """
        cleaned = _strip_sql_comments(query)
        object_type, target = _extract_target(cleaned, verb)

        # Which table to snapshot for a before/after schema diff.
        snapshot_target = target if (verb == 'alter' and object_type == 'table') else ''
        before = self.__snapshot_table(cursor, snapshot_target) if snapshot_target else None

        started = perf_counter()
        cursor.execute(query)
        elapsed_ms = (perf_counter() - started) * 1000

        rows_affected = max(cursor.rowcount, 0) if cursor.rowcount is not None else 0
        last_row_id = getattr(cursor, 'lastrowid', None)
        info = self.__connection_info()
        warnings = self.__fetch_warnings(cursor)

        self.__mysql.connection.commit()

        # An ALTER may have renamed the table; follow it for the after snapshot.
        if snapshot_target:
            _, renamed_table, _ = _parse_alter_actions(cleaned)
            if renamed_table:
                snapshot_target = renamed_table

        return self.__build_feedback(
            cursor=cursor,
            query=query,
            verb=verb,
            elapsed_ms=elapsed_ms,
            rows_affected=rows_affected,
            last_row_id=last_row_id if isinstance(last_row_id, int) and last_row_id > 0 else None,
            info=info,
            warnings=warnings,
            before=before,
            snapshot_target=snapshot_target,
        )

    def __add_execute_query(self) -> None:
        @self.__app.route('/execute_query', methods=['POST'])
        def execute_query() -> jsonify:
            try:
                data = request.get_json()
                operation = data.get('operation')
                query = data.get('query')
                result: dict[str, Any] = {}

                cursor = self.__mysql.connection.cursor()

                if query and query.strip():
                    query_type = _detect_verb(query)

                    if query_type in ['select', 'show', 'describe', 'explain', 'with', 'desc']:
                        cursor.execute(query)
                        rows = cursor.fetchall()
                        column_names = [desc[0] for desc in cursor.description or []]
                        if rows:
                            result["data"] = {f"row_{index + 1}": dict(zip(column_names, row)) for index, row in enumerate(rows)}
                        else:
                            result["message"] = "No data found."

                    elif query_type in ('insert', 'replace', 'update', 'delete', 'merge',
                                        'load', 'alter', 'rename', 'create', 'drop', 'truncate'):
                        result["feedback"] = self.__run_write_statement(cursor, query, query_type)

                    elif query_type == 'call':
                        started = perf_counter()
                        cursor.execute(query)
                        elapsed_ms = (perf_counter() - started) * 1000
                        # A procedure may return several result sets; drain them all.
                        all_rows = []
                        column_names = []
                        while True:
                            try:
                                batch = cursor.fetchall()
                            except Exception:
                                batch = []
                            if batch:
                                if not column_names:
                                    column_names = [desc[0] for desc in cursor.description or []]
                                all_rows.extend(batch)
                            try:
                                if not cursor.nextset():
                                    break
                            except Exception:
                                break
                        if all_rows:
                            result["data"] = {f"row_{index + 1}": dict(zip(column_names, row)) for index, row in enumerate(all_rows)}
                        else:
                            # Captured before COMMIT and SHOW WARNINGS, both of
                            # which reset the connection status for the call.
                            rows_affected = max(cursor.rowcount, 0) if cursor.rowcount is not None else 0
                            info = self.__connection_info()
                            warnings = self.__fetch_warnings(cursor)
                            self.__mysql.connection.commit()
                            result["feedback"] = self.__build_feedback(
                                cursor=cursor, query=query, verb=query_type,
                                elapsed_ms=elapsed_ms, rows_affected=rows_affected,
                                last_row_id=None, info=info, warnings=warnings,
                                before=None, snapshot_target='',
                            )

                    elif query_type == 'use':
                        try:
                            tokens = query.strip().rstrip(';').split()
                            if len(tokens) < 2:
                                result["message"] = "Invalid USE query: missing database name."
                            else:
                                requested_db = tokens[1].lower()
                                current_db = self.__database_name.lower()

                                if requested_db == current_db:
                                    result["message"] = "You are already using this database."
                                else:
                                    result["message"] = "Cannot switch databases dynamically. Please reconfigure"
                        except Exception as e:
                            return jsonify({'error': f'{e}'})

                    elif query_type == 'help':
                        return jsonify({"warning": "The help command is not supported yet"})

                    elif query_type:
                        result["feedback"] = self.__run_write_statement(cursor, query, query_type)

                    else:
                        return jsonify({"warning": "Custom query is required"}), 400

                elif operation:
                    if operation == "insert":
                        table_name = data.get('table_name')
                        columns = data.get('columns')
                        values = data.get('values')
                        if not table_name or not columns or not values:
                            return jsonify({"warning": "Table name, columns, and values are required for insert"}), 400
                        query = f"INSERT INTO {table_name} ({columns}) VALUES ({values})"
                        result["feedback"] = self.__run_write_statement(cursor, query, 'insert')

                    elif operation == "delete":
                        table_name = data.get('table_name')
                        condition = data.get('condition')
                        if not table_name or not condition:
                            return jsonify({"warning": "Table name and condition are required for delete"}), 400
                        query = f"DELETE FROM {table_name} WHERE {condition}"
                        result["feedback"] = self.__run_write_statement(cursor, query, 'delete')

                    elif operation == "update":
                        table_name = data.get('table_name')
                        field = data.get('field')
                        condition = data.get('condition')
                        if not table_name or not field or not condition:
                            return jsonify({"warning": "Table name, field, and condition are required for update"}), 400
                        query = f"UPDATE {table_name} SET {field} WHERE {condition}"
                        result["feedback"] = self.__run_write_statement(cursor, query, 'update')

                    elif operation == "fetch_data":
                        table_name = data.get('table_name')
                        if not table_name:
                            return jsonify({"warning": "Table name is required for fetch"}), 400
                        query = f'SELECT * FROM {table_name}'
                        cursor.execute(query)
                        rows = cursor.fetchall()
                        column_names = [desc[0] for desc in cursor.description or []]
                        if rows:
                            result["data"] = {f"row_{index + 1}": dict(zip(column_names, row)) for index, row in enumerate(rows)}
                        else:
                            result["message"] = "No data found."

                    elif operation == "show_tables":
                        cursor.execute("SHOW TABLES;")
                        tables = cursor.fetchall()
                        if tables:
                            result["tables"] = {f"table_{index + 1}": table[0] for index, table in enumerate(tables)}
                        else:
                            result['message'] = "No tables found."
                    else:
                        return jsonify({"error": "Invalid operation"}), 400

                else:
                    return jsonify({"warning": "Custom query is required"}), 400

                cursor.close()

                return jsonify(result)

            except Exception as e:
                return jsonify({'error': f'{e}'}), 500

    def __discover_cert_names(self) -> list[str]:
        """Hostnames/IPs the TLS certificate should cover: localhost, the
        machine hostname and all LAN IPs (so https://<lan-ip>:port works)."""
        import socket
        names = ["localhost", "127.0.0.1", "::1"]
        ips: set[str] = set()
        try:
            hostname = socket.gethostname()
            if hostname and hostname != "localhost":
                names.append(hostname)
            ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except Exception:
            pass
        try:  # outbound interface IP (no packets are sent)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
        for ip in sorted(ips):
            if ip not in names:
                names.append(ip)
        return names

    def __get_mkcert_context(self, ssl_dir: str, names: list[str]) -> tuple[str, str] | None:
        """Issue a locally-trusted certificate via mkcert (if installed).

        mkcert creates its own local CA and installs it into the system
        trust stores, so browsers trust the certificate with zero warnings.
        Returns None when mkcert is not on PATH or any step fails (the
        caller then falls back to a self-signed certificate).
        """
        import shutil
        import subprocess
        if not shutil.which("mkcert"):
            return None
        cert_path = os.path.join(ssl_dir, "mkcert-cert.pem")
        key_path = os.path.join(ssl_dir, "mkcert-key.pem")
        names_file = os.path.join(ssl_dir, "mkcert-names.txt")
        wanted = sorted(set(names))
        try:
            current: list[str] = []
            if os.path.exists(names_file):
                with open(names_file) as f:
                    current = sorted(line.strip() for line in f if line.strip())
            if wanted != current or not (os.path.exists(cert_path) and os.path.exists(key_path)):
                os.makedirs(ssl_dir, exist_ok=True)
                print("\U0001f510 Installing mkcert local CA into system trust stores "
                      "(one-time per machine)\u2026")
                subprocess.run(["mkcert", "-install"],
                               capture_output=True, text=True, timeout=120, check=True)
                subprocess.run(["mkcert", "-cert-file", cert_path,
                                "-key-file", key_path, *wanted],
                               capture_output=True, text=True, timeout=120, check=True)
                with open(names_file, "w") as f:
                    f.write("\n".join(wanted) + "\n")
                print(f"\U0001f510 mkcert certificate ready at {cert_path} "
                      f"(locally trusted, no browser warnings)")
            return (cert_path, key_path)
        except Exception as e:
            print(f"\u26a0\ufe0f mkcert failed ({e}) \u2014 falling back to self-signed certificate")
            return None

    def __get_selfsigned_context(self, ssl_dir: str, names: list[str]) -> tuple[str, str] | None:
        """Generate (once) and return a self-signed (cert, key) pair covering
        ``names``. Stored under ``ssl_dir`` so it survives restarts; it is
        regenerated automatically when the machine's IPs/hostname change.
        Returns None when the ``cryptography`` package is unavailable.
        """
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
        except ImportError:
            return None

        import datetime
        import ipaddress

        def _san_entries() -> list:
            entries = []
            for name in names:
                try:
                    entry = x509.IPAddress(ipaddress.ip_address(name))
                except ValueError:
                    entry = x509.DNSName(name)
                if entry not in entries:
                    entries.append(entry)
            return entries

        def _san_values(cert) -> set:
            try:
                san = cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName).value
            except x509.ExtensionNotFound:
                return set()
            return set(san.get_values_for_type(x509.DNSName)) | {
                str(ip) for ip in san.get_values_for_type(x509.IPAddress)}

        def _wanted_values(entries) -> set:
            wanted = set()
            for e in entries:
                wanted.add(e.value if isinstance(e, x509.DNSName) else str(e.value))
            return wanted

        cert_path = os.path.join(ssl_dir, "cert.pem")
        key_path = os.path.join(ssl_dir, "key.pem")
        entries = _san_entries()

        def _needs_new_cert() -> bool:
            if not (os.path.exists(cert_path) and os.path.exists(key_path)):
                return True
            try:
                with open(cert_path, "rb") as f:
                    cert = x509.load_pem_x509_certificate(f.read())
                if cert.not_valid_after_utc <= datetime.datetime.now(datetime.timezone.utc):
                    return True
                # Regenerate when the machine's current IPs/hostname are not covered
                return not _wanted_values(entries) <= _san_values(cert)
            except Exception:
                return True

        if _needs_new_cert():
            os.makedirs(ssl_dir, exist_ok=True)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PyAwaish")])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now)
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(entries), critical=False)
                .sign(key, hashes.SHA256())
            )
            with open(key_path, "wb") as f:
                f.write(key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                ))
            os.chmod(key_path, 0o600)
            with open(cert_path, "wb") as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))
            print(f"\U0001f510 Generated a self-signed certificate at {cert_path} "
                  f"(covers localhost, hostname and LAN IPs)")
            import shutil
            if not shutil.which("mkcert"):
                print("\U0001f4a1 Tip: install mkcert (https://github.com/FiloSottile/mkcert) "
                      "and run 'mkcert -install' once for locally-trusted "
                      "certificates with no browser warnings.")

        return (cert_path, key_path)

    def __get_ssl_context(self) -> tuple[str, str] | None:
        """Return a (cert_path, key_path) pair for HTTPS.

        Prefers a locally-trusted certificate via mkcert when it is
        installed; otherwise generates a persistent self-signed certificate.
        Returns None when neither is available (caller falls back to HTTP).
        """
        ssl_dir = os.path.join(os.path.expanduser("~"), ".pyawaish", "ssl")
        names = self.__discover_cert_names()
        return (self.__get_mkcert_context(ssl_dir, names)
                or self.__get_selfsigned_context(ssl_dir, names))


    def execute(self, debug_mode: bool = False, port_number: int = 5001,
                host_address: str = "0.0.0.0", use_ssl: bool = True,
                ssl_cert: str | None = None, ssl_key: str | None = None) -> None:
        """Start the Flask server. HTTPS is used by default.

        - **use_ssl (bool):** Serve over HTTPS (default: True). Pass
          ``use_ssl=False`` to fall back to plain HTTP.
        - **ssl_cert / ssl_key (str | None):** Paths to your own certificate
          and private key. When omitted, the app prefers mkcert (if
          installed) to issue a locally-trusted certificate with zero
          browser warnings; otherwise a persistent self-signed certificate
          is generated automatically on first run (stored in
          ``~/.pyawaish/ssl/``).
        """
        ssl_context = None
        scheme = "http"
        if ssl_cert and ssl_key:
            ssl_context = (ssl_cert, ssl_key)
            scheme = "https"
        elif use_ssl:
            ssl_context = self.__get_ssl_context()
            if ssl_context:
                scheme = "https"
            else:
                print("⚠️ 'cryptography' is not installed — HTTPS is unavailable, "
                      "falling back to HTTP. Install it with: pip install cryptography")

        display_host = "localhost" if host_address in ("0.0.0.0", "::") else host_address
        print(f"🚀 PyAwaish running at {scheme}://{display_host}:{port_number}")
        self.__app.run(debug=debug_mode, port=port_number, host=host_address,
                       ssl_context=ssl_context)
