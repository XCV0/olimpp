"""Mini App в личке бота: по каждому доступному объекту — этап, «светофор», задачи, проблемы, договорённости.

Безопасность: каждый запрос несёт initData от Telegram, подпись проверяется токеном бота (HMAC-SHA256),
user_id берётся только из проверенной initData. Доступ к объектам — как у бота в личке:
руководитель видит все, сотрудник — объекты, в чатах которых он писал или где на нём задачи.
"""

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from . import approvals, db, notify, people, stages, tasks
from .config import config
from .formatting import CONF_ICON, TYPE_NAME, esc, icon, is_overdue, msg_link, short_date, status_text

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "webapp"
INIT_DATA_TTL = 24 * 3600


def verify_init_data(init_data: str) -> dict | None:
    """Проверка подписи Telegram WebApp initData. Возвращает user или None."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", config.bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(pairs.get("auth_date", 0)) > INIT_DATA_TTL:
        return None
    try:
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None


async def _user_objects(uid: int, username: str) -> tuple[bool, list[dict]]:
    if await db.is_manager(uid):
        return True, await db.all_objects()
    # сотрудник видит объекты (топики), где писал сам или где на нём задачи
    return False, await db.fetchall(
        "SELECT DISTINCT o.* FROM objects o WHERE EXISTS (SELECT 1 FROM messages ms WHERE ms.user_id = ? "
        "AND ms.chat_id = o.chat_id AND COALESCE(ms.thread_id, 0) = o.thread_id) "
        "OR o.id IN (SELECT object_id FROM records WHERE type = 'task' AND (assignee_user_id = ? OR lower(assignee) = ?))",
        uid, str(uid), f"@{username.lower()}" if username else "",
    )


def _item(r: dict) -> dict:
    return {
        "id": r["id"], "type": r["type"], "icon": icon(r), "title": r["title"] or r["value"][:60],
        "value": r["value"], "status": status_text(r), "assignee": r.get("assignee"),
        "due": r.get("due_date"), "author": r["author_name"], "date": short_date(r["created_at"]),
        "source": "✍️" if r.get("source") == "tag" else "👁", "note": r.get("note"),
        "conf": CONF_ICON.get(r["confidence"], ""), "link": msg_link(r["chat_id"], r["message_id"]),
        "overdue": is_overdue(r), "can_done": r["type"] == "task" and r["status"] in ("open", "accepted"),
        "can_confirm": r["type"] == "agreement" and (r["status"] == "disputed"
                                                    or (r["status"] == "active" and r["confidence"] != "confirmed")),
        "can_close": r["type"] in ("problem", "question") and r["status"] == "open",
        "kind": TYPE_NAME.get(r["type"], r["type"]),
    }


def _is_mine(r: dict, uid: int, username: str, first_name: str) -> bool:
    a = (r.get("assignee") or "").lower()
    return str(r.get("assignee_user_id") or "") == str(uid) or (username and a == f"@{username.lower()}") \
        or (first_name and first_name.lower() in a)


async def _object_payload(o: dict, uid: int, username: str, first_name: str) -> dict:
    recs = await db.records_for_object(o["id"])
    stage = await db.current_stage(o["id"])
    hist = await db.stage_history(o["id"], 6)
    task_list = [r for r in recs if r["type"] == "task"]
    problems = [r for r in recs if r["type"] == "problem"]
    agreements = [r for r in recs if r["type"] == "agreement"]
    questions = [r for r in recs if r["type"] == "question"]
    disputed = [r for r in agreements if r["status"] == "disputed"]
    pending = [r for r in agreements if r["confidence"] == "reported" and r["status"] == "active"]
    overdue = [r for r in task_list if is_overdue(r)]
    mine = [r for r in task_list if _is_mine(r, uid, username, first_name)]
    light = "red" if disputed or overdue or problems else "yellow" if pending or questions else "green"
    last = await db.fetchone("SELECT MAX(date) AS d FROM messages WHERE chat_id = ? AND COALESCE(thread_id, 0) = ?",
                             o["chat_id"], o["thread_id"])
    # лента: последние события объекта — новые записи любых статусов и смены этапа
    feed = [{"at": r["created_at"], **_item(r)} for r in await db.fetchall(
        "SELECT * FROM records WHERE object_id = ? ORDER BY id DESC LIMIT 30", o["id"])]
    feed += [{"at": h["created_at"], "type": "stage", "icon": "🏗", "title": f"Этап: {h['stage']}",
              "author": h["user_name"], "date": short_date(h["created_at"]), "kind": "Этап",
              "link": msg_link(h["chat_id"], h["message_id"])} for h in hist]
    feed.sort(key=lambda e: e["at"], reverse=True)
    return {
        "id": o["id"], "name": o["name"], "address": o.get("address"), "description": o.get("description"),
        "stage": stage["stage"] if stage else None,
        "stage_since": short_date(stage["created_at"]) if stage else None,
        "stage_by": stage["user_name"] if stage else None,
        "stage_history": [{"stage": h["stage"], "date": short_date(h["created_at"]), "by": h["user_name"]} for h in hist],
        "light": light,
        "counts": {"tasks": len(task_list), "mine": len(mine), "overdue": len(overdue), "problems": len(problems),
                   "pending": len(pending), "disputed": len(disputed), "questions": len(questions)},
        "last_activity": short_date(last["d"]) if last and last["d"] else None,
        "mine": [_item(r) for r in mine],
        "tasks": [_item(r) for r in sorted(task_list, key=lambda r: (not is_overdue(r), r.get("due_date") or "9999"))],
        "problems": [_item(r) for r in problems],
        "agreements": [_item(r) for r in sorted(agreements, key=lambda r: r["status"] != "disputed")],
        "questions": [_item(r) for r in questions],
        "feed": feed[:30],
    }


async def _auth(request: web.Request) -> dict:
    body = await request.json()
    user = verify_init_data(body.get("initData", ""))
    if not user and config.webapp_dev_user and request.remote in ("127.0.0.1", "::1"):
        user = {"id": config.webapp_dev_user, "first_name": "dev"}  # только локальная отладка в браузере
    if not user:
        raise web.HTTPUnauthorized(text="bad initData")
    return user


async def api_data(request: web.Request) -> web.Response:
    user = await _auth(request)
    uid, uname, fname = int(user["id"]), user.get("username") or "", user.get("first_name") or ""
    is_manager, objects = await _user_objects(uid, uname)
    payload = [await _object_payload(o, uid, uname, fname) for o in objects]
    order = {"red": 0, "yellow": 1, "green": 2}
    payload.sort(key=lambda p: (order[p["light"]], p["name"]))
    return web.json_response({
        "user": {"name": fname, "manager": is_manager, "id": uid},
        "employees": [{"id": u["user_id"], "fio": u["fio"], "username": u.get("username")} for u in await db.employees()],
        "generated": datetime.now(config.tz).strftime("%d.%m %H:%M"),
        "objects": payload,
    })


async def api_done(request: web.Request) -> web.Response:
    user = await _auth(request)
    body = await request.json()
    uid = int(user["id"])
    r = await db.get_record(int(body.get("id", 0)))
    is_manager, objects = await _user_objects(uid, user.get("username") or "")
    if not r or r["type"] != "task" or r["object_id"] not in {o["id"] for o in objects}:
        raise web.HTTPForbidden(text="нет доступа")
    changed, _ = await tasks.apply("done", [r], uid, user.get("first_name") or "Mini App")
    return web.json_response({"ok": bool(changed)})


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def _access(request: web.Request, object_id: int) -> tuple[dict, dict, bool]:
    """Пользователь, объект и флаг руководителя — или 403, если объект не его."""
    user = await _auth(request)
    is_manager, objects = await _user_objects(int(user["id"]), user.get("username") or "")
    obj = next((o for o in objects if o["id"] == object_id), None)
    if not obj:
        raise web.HTTPForbidden(text="нет доступа к объекту")
    return user, obj, is_manager


def _uname(user: dict) -> str:
    return " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "Mini App"


async def _post_chat(request: web.Request, obj: dict, text: str, kb=None):
    """Изменения из Mini App видит вся бригада — бот пишет о них в чат объекта."""
    bot = request.app.get("bot")
    if bot:
        try:
            return await notify.to_object(bot, obj, text, reply_markup=kb)
        except Exception as e:
            log.warning("не смог написать в чат объекта: %s", e)


async def api_stage(request: web.Request) -> web.Response:
    body = await request.json()
    user, obj, _ = await _access(request, int(body.get("object_id", 0)))
    stage = (body.get("stage") or "").strip()[:80]
    if not stage:
        raise web.HTTPBadRequest(text="пустой этап")
    stage = stage[:1].upper() + stage[1:]
    msg = await _post_chat(request, obj, f"🏗 {esc(_uname(user))} сменил этап в Mini App: «<b>{esc(stage)}</b>»")
    text = await stages.change(request.app["bot"], obj, stage, int(user["id"]), _uname(user),
                               obj["chat_id"], msg.message_id if msg else 0)
    return web.json_response({"ok": True, "text": text})


async def api_task(request: web.Request) -> web.Response:
    """Новая задача из Mini App: та же карточка в чате объекта, личка исполнителю, защита от дублей."""
    from datetime import timezone
    from .modes import _notify_assignee, card, done_kb

    body = await request.json()
    user, obj, _ = await _access(request, int(body.get("object_id", 0)))
    title = (body.get("title") or "").strip()[:200]
    if not title:
        raise web.HTTPBadRequest(text="опишите задачу")
    due = (body.get("due") or "").strip()[:16] or None
    assignee_user = await db.get_user(int(body["assignee_id"])) if body.get("assignee_id") else \
        await people.resolve((body.get("assignee") or "").strip()[:80])
    username = assignee_user.get("username") if assignee_user else None
    assignee = (people.short(assignee_user.get("fio")) or assignee_user.get("name")) if assignee_user \
        else ((body.get("assignee") or "").strip()[:80] or None)
    rec = dict(
        object_id=obj["id"], type="task", title=title[:60], value=title, confidence="reported", status="open",
        assignee=assignee,
        assignee_user_id=assignee_user["user_id"] if assignee_user else None,
        due_date=due.replace("T", " ") if due else None, author_id=int(user["id"]), author_name=_uname(user),
        chat_id=obj["chat_id"], message_id=0, created_at=datetime.now(timezone.utc).isoformat(),
        note="создано в Mini App", source="tag",
    )
    if dup := await tasks.find_duplicate(obj["id"], rec):
        await tasks.merge_into(dup, rec)
        return web.json_response({"ok": True, "duplicate": True, "id": dup["id"]})
    f = {"title": title[:60], "action": title, "assignee_username": username, "assignee": assignee,
         "due_date": due[:10] if due else None, "due_time": due[11:16] if due and len(due) > 10 else None}
    rid = await db.add_record(**rec)
    r = await db.get_record(rid)
    sent = await _post_chat(request, obj, f"Задача из Mini App от {esc(_uname(user))}:\n\n" + card(r, obj, f), done_kb(rid))
    if sent:  # карточка в чате становится «исходным сообщением» — на неё работают ссылки и реплай «сделал»
        await db.update_record(rid, message_id=sent.message_id, card_message_id=str(sent.message_id))
        r = await db.get_record(rid)
        await _notify_assignee(request.app["bot"], sent, r, obj, f)
    return web.json_response({"ok": True, "id": rid})


async def api_agreement(request: web.Request) -> web.Response:
    body = await request.json()
    r = await db.get_record(int(body.get("id", 0)))
    if not r or r["type"] != "agreement":
        raise web.HTTPNotFound()
    user, obj, is_manager = await _access(request, r["object_id"])
    if not is_manager:
        raise web.HTTPForbidden(text="подтверждать договорённости может только руководитель")
    if body.get("action") == "confirm":
        await db.update_record(r["id"], confidence="confirmed", status="active", note=f"подтвердил {_uname(user)} (Mini App)")
        if r["supersedes"]:
            await db.update_record(r["supersedes"], status="superseded")
        await _post_chat(request, obj, f"🟢 Руководитель подтвердил: <b>{esc(r['title'])}</b> — {esc(r['value'])}")
    else:
        await db.update_record(r["id"], status="rejected", note=f"отклонил {_uname(user)} (Mini App)")
        if r["supersedes"]:
            await db.update_record(r["supersedes"], status="active")
        await _post_chat(request, obj, f"❌ Руководитель отклонил: <b>{esc(r['title'])}</b>")
    return web.json_response({"ok": True})


async def api_close(request: web.Request) -> web.Response:
    """Закрыть проблему или вопрос — любой участник объекта."""
    body = await request.json()
    r = await db.get_record(int(body.get("id", 0)))
    if not r or r["type"] not in ("problem", "question"):
        raise web.HTTPNotFound()
    user, _, _ = await _access(request, r["object_id"])
    note = f"закрыто: {_uname(user)} (Mini App)"
    await db.update_record(r["id"], status="closed", note=f"{r['note']}; {note}" if r.get("note") else note)
    return web.json_response({"ok": True})


async def api_profile(request: web.Request) -> web.Response:
    body = await request.json()
    user, obj, is_manager = await _access(request, int(body.get("object_id", 0)))
    if not is_manager:
        raise web.HTTPForbidden(text="профиль объекта меняет руководитель")
    await db.update_object(obj["id"], address=(body.get("address") or "").strip()[:200] or None,
                           description=(body.get("description") or "").strip()[:500] or None)
    return web.json_response({"ok": True})


async def api_approval(request: web.Request) -> web.Response:
    """Запомнить, какие пункты ушли клиенту в листе согласования."""
    body = await request.json()
    user, obj, _ = await _access(request, int(body.get("object_id", 0)))
    ids = [int(i) for i in body.get("ids") or []][:50]
    rid = await approvals.register(obj["id"], ids, int(user["id"]))
    return web.json_response({"ok": True, "id": rid})


def build_app(bot=None) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", index)
    app.router.add_post("/api/data", api_data)
    app.router.add_post("/api/done", api_done)
    app.router.add_post("/api/stage", api_stage)
    app.router.add_post("/api/task", api_task)
    app.router.add_post("/api/agreement", api_agreement)
    app.router.add_post("/api/close", api_close)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/approval", api_approval)
    return app


async def start(bot=None) -> web.AppRunner:
    runner = web.AppRunner(build_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, config.webapp_host, config.webapp_port).start()
    log.info("Mini App слушает %s:%s", config.webapp_host, config.webapp_port)
    return runner
