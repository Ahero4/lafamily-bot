"""Согласованные копии SQLite и проверка архивов без распаковки произвольных путей."""
import hashlib
import json
import sqlite3
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from lafamily.errors import UserError
from lafamily.modules.events.time import timestamp

MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
class FatalRestoreError(UserError):
    pass


ARCHIVE_KIND = 'lafamily-database-backup'


def snapshot(connection, target):
    target = Path(target)
    if target.exists():
        raise UserError('Файл копии уже существует.')
    copy = sqlite3.connect(target)
    try:
        connection.backup(copy)
        if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise UserError('Копия базы не прошла проверку целостности.')
    finally:
        copy.close()
    if target.stat().st_size > MAX_DATABASE_BYTES:
        raise UserError('База превышает лимит автоматического архива: 256 МБ.')


def database_summary(database):
    connection = sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True)
    try:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts = {name: connection.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in tables}
        reports, active, cancelled = connection.execute('SELECT COUNT(*), COALESCE(SUM(cancelled_at IS NULL),0), COALESCE(SUM(cancelled_at IS NOT NULL),0) FROM reports').fetchone()
        participants = connection.execute('SELECT COUNT(DISTINCT user_id) FROM report_members').fetchone()[0]
        active_members,points = connection.execute('SELECT COUNT(DISTINCT m.user_id),COALESCE(SUM(r.points+CASE WHEN m.user_id=r.mvp_id THEN r.mvp_bonus ELSE 0 END),0) FROM reports r JOIN report_members m ON m.report_id=r.id WHERE r.cancelled_at IS NULL').fetchone()
        subscribers = connection.execute('SELECT COUNT(DISTINCT user_id) FROM price_subscriptions').fetchone()[0]
        return {'tables':counts,'records':sum(counts.values()),'reports':reports,'active':active,'cancelled':cancelled,
                'participants':participants,'active_participants':active_members,'points':points,'subscribers':subscribers,
                'database_bytes':Path(database).stat().st_size}
    finally:
        connection.close()


def pack_database(database, archive, guild_id, bot_id, version, kind='scheduled'):
    database, archive = Path(database), Path(archive)
    manifest = {'kind': ARCHIVE_KIND, 'format': 1, 'guild_id': str(guild_id), 'bot_id': str(bot_id),
                'version': version, 'created_at': timestamp(), 'reason': kind,
                'sha256': hashlib.sha256(database.read_bytes()).hexdigest(), 'summary': database_summary(database)}
    with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as output:
        output.write(database, 'lafamily.sqlite')
        output.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
    return manifest


def inspect_archive(archive, target, guild_id, bot_id, version):
    archive, target = Path(archive), Path(target)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise UserError('Архив превышает лимит 25 МБ.')
    try:
        with ZipFile(archive) as source:
            entries = source.infolist()
            if len(entries) != 2 or {e.filename for e in entries} != {'manifest.json', 'lafamily.sqlite'}:
                raise UserError('Нужен архив бота с manifest.json и lafamily.sqlite, без других файлов.')
            if any(e.flag_bits & 1 for e in entries):
                raise UserError('Зашифрованные архивы не поддерживаются.')
            if source.getinfo('manifest.json').file_size > 16384:
                raise UserError('Некорректный манифест архива.')
            size = source.getinfo('lafamily.sqlite').file_size
            if not 100 <= size <= MAX_DATABASE_BYTES:
                raise UserError('Размер базы в архиве недопустим.')
            manifest = json.loads(source.read('manifest.json'))
            if (not isinstance(manifest, dict) or manifest.get('kind') != ARCHIVE_KIND
                    or manifest.get('format') != 1 or manifest.get('guild_id') != str(guild_id)
                    or manifest.get('bot_id') != str(bot_id)):
                raise UserError('Архив создан не для этого сервера или бота.')
            saved_version = tuple(map(int, manifest['version'].split('.')))
            current_version = tuple(map(int, version.split('.')))
            if len(saved_version) != 3 or saved_version > current_version:
                raise UserError('Архив из более новой версии. Сначала обновите бота.')
            data = source.read('lafamily.sqlite')
            if len(data) != size or hashlib.sha256(data).hexdigest() != manifest.get('sha256'):
                raise UserError('Контрольная сумма базы не совпала.')
            if not data.startswith(b'SQLite format 3\x00'):
                raise UserError('В архиве нет базы SQLite.')
            if target.exists():
                raise UserError('Проверочная копия уже существует.')
            target.write_bytes(data)
    except (BadZipFile, KeyError, TypeError, ValueError, UnicodeError, RuntimeError) as error:
        raise UserError('Архив повреждён или имеет неизвестный формат.') from error
    connection = sqlite3.connect(target.resolve().as_uri()+'?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise UserError('База не прошла проверку целостности.')
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
            raise UserError('Архив содержит неподдерживаемые представления или триггеры.')
        required = {'settings': {'guild_id','channel_id'}, 'reports': {'id','guild_id','points','occurred_at','cancelled_at'},
                    'report_members': {'report_id','user_id'}, 'audit_log': {'guild_id','action','created_at'}}
        for table, columns in required.items():
            existing = {r[1] for r in connection.execute(f'PRAGMA table_info({table})')}
            if not columns.issubset(existing):
                raise UserError('Схема базы не соответствует LaFamily Bot.')
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise UserError('В базе повреждены связи между таблицами.')
        guilds = {r[0] for r in connection.execute('SELECT guild_id FROM settings UNION SELECT guild_id FROM reports')}
        if guilds - {str(guild_id)}:
            raise UserError('В базе обнаружены данные другого сервера.')
        summary = dict(zip(('reports','active','cancelled'), connection.execute('''SELECT COUNT(*),
            COALESCE(SUM(cancelled_at IS NULL),0), COALESCE(SUM(cancelled_at IS NOT NULL),0) FROM reports''').fetchone()))
        summary['participants'] = connection.execute('SELECT COUNT(DISTINCT user_id) FROM report_members').fetchone()[0]
    except sqlite3.DatabaseError as error:
        raise UserError('Не удалось прочитать проверочную базу.') from error
    finally:
        connection.close()
    # Миграции применяются только к отдельной проверочной копии.
    from lafamily.database import Store
    migrated = Store(target)
    migrated.close()
    return manifest, summary


def copy_into_existing(source, target):
    source, target = Path(source), Path(target)
    original = sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)
    try:
        destination = sqlite3.connect(target.resolve().as_uri()+'?mode=rw', uri=True, timeout=10)
        try:
            original.backup(destination)
        finally:
            destination.close()
    finally:
        original.close()


def apply_restore(request, target):
    """Вызывается только после закрытия старого клиента и DatabaseWorker."""
    from lafamily.database import Store
    staged = Store(request.database)
    try:
        staged.configure_database_backups(request.guild_id, request.actor_id, request.channel_id)
        staged.audit(request.guild_id, request.actor_id, 'database.restored',
                     {'source_message_id': request.message_id, 'safety_message_id': request.safety_message_id})
    finally:
        staged.close()
    try:
        copy_into_existing(request.database, target)
    except Exception as original_error:
        # Возврат к точной копии перед восстановлением при ошибке замены.
        try:
            copy_into_existing(request.safety_database, target)
        except Exception as rollback_error:
            raise FatalRestoreError('Восстановление и возврат к страховочной копии не завершены. Остановите запуск и используйте страховочный архив.') from rollback_error
        raise original_error
