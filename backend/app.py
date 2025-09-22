# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import random
import time
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

logging.basicConfig(level=logging.INFO)

API_BASE_URL = "https://edusp-api.ip.tv"
CLIENT_ORIGIN = "https://trollchipss-tarefas.vercel.app"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"

def default_headers(extra=None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-realm": "edusp",
        "x-api-platform": "webclient",
        "User-Agent": USER_AGENT,
        "Origin": CLIENT_ORIGIN,
        "Referer": CLIENT_ORIGIN + "/",
    }
    if extra:
        headers.update(extra)
    return headers

# ---------- AUTH ----------
@app.route("/auth", methods=["POST"])
def auth():
    data = request.get_json(force=True)
    ra = data.get("ra") or data.get("login")
    senha = data.get("password") or data.get("senha")
    if not ra or not senha:
        return jsonify({"success": False, "message": "RA e senha obrigatórios"}), 400

    payload = {"realm": "edusp", "platform": "webclient", "id": ra, "password": senha}

    try:
        r = requests.post(f"{API_BASE_URL}/registration/edusp", json=payload, headers=default_headers(), timeout=15)
        if r.status_code != 200:
            logging.warning("auth fail: %s %s", r.status_code, r.text[:200])
            return jsonify({"success": False, "message": "Falha no login", "detail": r.text}), r.status_code
        j = r.json()
        return jsonify({
            "success": True,
            "user_info": {
                "auth_token": j.get("auth_token"),
                "nick": j.get("nick"),
                "external_id": j.get("external_id"),
                "name": j.get("name", "")
            },
            # compatibilidade com frontend antigo que esperava auth_token top-level:
            "auth_token": j.get("auth_token"),
            "nick": j.get("nick")
        })
    except requests.RequestException as e:
        logging.exception("auth exception")
        return jsonify({"success": False, "message": str(e)}), 500

# ---------- ROOMS helper ----------
def fetch_rooms(token):
    r = requests.get(f"{API_BASE_URL}/room/user?list_all=true&with_cards=true",
                     headers=default_headers({"x-api-key": token}),
                     timeout=15)
    r.raise_for_status()
    return r.json()

# ---------- TASKS ----------
@app.route("/tasks", methods=["POST"])
def tasks_generic():
    """
    POST /tasks
    body { auth_token, filter }  filter in ['pending','expired']
    """
    try:
        data = request.get_json(force=True)
        token = data.get("auth_token")
        task_filter = data.get("filter", "pending")
        if not token:
            return jsonify({"success": False, "message": "Token é obrigatório"}), 400

        rooms = fetch_rooms(token)
        targets = set()
        for room in rooms.get("rooms", []):
            if "id" in room:
                targets.add(str(room["id"]))
            if "name" in room:
                targets.add(room["name"])

        if not targets:
            return jsonify({"success": True, "tasks": [], "count": 0, "message": "Nenhuma sala encontrada"})

        base_params = {
            "limit": 100,
            "offset": 0,
            "is_exam": "false",
            "with_answer": "true",
            "is_essay": "false",
            "with_apply_moment": "true",
        }
        if task_filter == "expired":
            base_params["expired_only"] = "true"
            base_params["filter_expired"] = "false"
        else:
            base_params["expired_only"] = "false"
            base_params["filter_expired"] = "true"

        tasks_found = []
        for target in list(targets):
            params = dict(base_params)
            params["publication_target"] = target
            try:
                r = requests.get(f"{API_BASE_URL}/tms/task/todo", params=params,
                                 headers=default_headers({"x-api-key": token}), timeout=15)
                if r.status_code == 200:
                    payload = r.json()
                    # API sometimes returns tasks as list or {"tasks": [...]}
                    if isinstance(payload, list):
                        tasks_found.extend(payload)
                    elif isinstance(payload, dict) and "tasks" in payload:
                        tasks_found.extend(payload.get("tasks", []))
                    else:
                        # try common keys
                        tasks_found.extend(payload if isinstance(payload, list) else [])
            except requests.RequestException:
                logging.exception("Failed fetching tasks for target %s", target)
                continue

        return jsonify({"success": True, "tasks": tasks_found, "count": len(tasks_found)})
    except Exception as e:
        logging.exception("tasks error")
        return jsonify({"success": False, "message": str(e)}), 500

# Backwards-compat routes used by frontend variations:
@app.route("/tasks/pending", methods=["POST"])
def tasks_pending():
    data = request.get_json(force=True)
    data["filter"] = "pending"
    return tasks_generic()

@app.route("/tasks/expired", methods=["POST"])
def tasks_expired():
    data = request.get_json(force=True)
    data["filter"] = "expired"
    # call generic but ensure body is forwarded; easier to reuse code: just call tasks_generic
    return tasks_generic()

# ---------- PROCESS A SINGLE TASK ----------
def prepare_answer_for_question(q):
    qtype = q.get("type")
    qid = q.get("id")
    result = {"question_id": qid, "question_type": qtype, "answer": None}

    try:
        if qtype == "multiple_choice":
            options = q.get("options", [])
            # If API indicates correct option use it
            correct = [o for o in options if o.get("correct")]
            if correct:
                result["answer"] = {str(correct[0].get("id")): True}
            else:
                # pick first option
                if options:
                    result["answer"] = {str(options[0].get("id")): True}
                else:
                    result["answer"] = {}
        elif qtype == "order-sentences":
            if q.get("options", {}).get("sentences"):
                result["answer"] = [s.get("value") for s in q["options"]["sentences"]]
            else:
                result["answer"] = []
        elif qtype == "fill-words":
            # choose placeholder words from phrase alternating items
            phrase = q.get("options", {}).get("phrase", [])
            if phrase:
                result["answer"] = [it.get("value") for i, it in enumerate(phrase) if i % 2 == 1]
            else:
                result["answer"] = []
        elif qtype == "text_ai" or qtype == "essay" or qtype == "text":
            # remove HTML naive and send placeholder
            txt = q.get("comment") or q.get("options", {}).get("text") or ""
            # simple strip tags
            import re
            clean = re.sub('<[^<]+?>', '', txt).strip()
            result["answer"] = {"0": clean or "Resposta automática"}
        elif qtype == "cloud":
            result["answer"] = q.get("options", {}).get("ids", [])
        elif qtype == "fill-letters":
            # if an answer is available
            if "options" in q and "answer" in q["options"]:
                result["answer"] = q["options"]["answer"]
            else:
                result["answer"] = {}
        else:
            # generic: map option id -> answer flag
            opts = q.get("options")
            if isinstance(opts, dict):
                out = {}
                for k, v in opts.items():
                    out[k] = v.get("answer", False) if isinstance(v, dict) else False
                result["answer"] = out
            else:
                result["answer"] = {}
    except Exception as e:
        logging.exception("prepare_answer error for question %s", qid)
        result["answer"] = {}

    return result

def process_one_task_sync(token, task_obj, time_min=1, time_max=3, is_draft=False):
    """
    Obtém a tarefa /tms/task/{id}, gera respostas e submete /tms/task/{id}/answer
    retorna dict com success/message/result
    """
    try:
        task_id = task_obj.get("id")
        if not task_id:
            return {"success": False, "message": "task sem id", "task_id": None}

        # get full task info
        r = requests.get(f"{API_BASE_URL}/tms/task/{task_id}", headers=default_headers({"x-api-key": token}), timeout=15)
        r.raise_for_status()
        task_info = r.json()

        questions = task_info.get("questions", [])
        answers_payload = {}
        for q in questions:
            if q.get("type") == "info":
                continue
            parsed = prepare_answer_for_question(q)
            # map to expected payload shape per original code: novoJson.answers[questionId] = { question_id, question_type, answer }
            answers_payload[str(parsed["question_id"])] = {
                "question_id": parsed["question_id"],
                "question_type": parsed["question_type"],
                "answer": parsed["answer"]
            }

        # random processing time between time_min and time_max in minutes -> seconds
        sec_min = max(1, int(time_min)) * 60
        sec_max = max(1, int(time_max)) * 60
        processing_time = random.randint(sec_min, sec_max)
        # For quicker testing you can uncomment next line to reduce sleep:
        # processing_time = random.randint(1,3)

        logging.info("Processing task %s (sleep %s seconds)", task_id, processing_time)
        time.sleep(processing_time)

        payload = {"answers": answers_payload, "final": not is_draft, "status": "draft" if is_draft else "submitted"}
        submit_url = f"{API_BASE_URL}/tms/task/{task_id}/answer"
        resp = requests.post(submit_url, headers=default_headers({"x-api-key": token}), json=payload, timeout=30)
        resp.raise_for_status()
        return {"success": True, "message": "Enviado", "task_id": task_id, "result": resp.json(), "processing_time": processing_time}
    except requests.HTTPError as he:
        logging.exception("HTTP error processing task %s", task_obj.get("id"))
        return {"success": False, "message": f"HTTP error: {str(he)}", "task_id": task_obj.get("id")}
    except Exception as e:
        logging.exception("Error processing task %s", task_obj.get("id"))
        return {"success": False, "message": str(e), "task_id": task_obj.get("id")}

@app.route("/task/process", methods=["POST"])
def task_process_route():
    try:
        data = request.get_json(force=True)
        token = data.get("auth_token")
        task = data.get("task")
        time_min = int(data.get("time_min", 1))
        time_max = int(data.get("time_max", 3))
        is_draft = bool(data.get("is_draft", False))

        if not token or not task:
            return jsonify({"success": False, "message": "Token e dados da tarefa obrigatórios"}), 400

        res = process_one_task_sync(token, task, time_min, time_max, is_draft)
        return jsonify(res)
    except Exception as e:
        logging.exception("task_process_route error")
        return jsonify({"success": False, "message": str(e)}), 500

# ---------- COMPLETE multiple tasks (parallel) ----------
@app.route("/complete", methods=["POST"])
def complete_tasks():
    try:
        data = request.get_json(force=True)
        token = data.get("auth_token")
        tasks = data.get("tasks", [])
        time_min = int(data.get("time_min", 1))
        time_max = int(data.get("time_max", 3))
        is_draft = bool(data.get("is_draft", False))

        if not token or not tasks:
            return jsonify({"success": False, "message": "Token e tarefas obrigatórios"}), 400

        results = []
        # use threadpool to run tasks in parallel (but avoid too many concurrent threads)
        max_workers = min(6, max(1, len(tasks)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_one_task_sync, token, t, time_min, time_max, is_draft) for t in tasks]
            for f in as_completed(futures):
                try:
                    results.append(f.result())
                except Exception as e:
                    logging.exception("thread error")
                    results.append({"success": False, "message": str(e)})

        return jsonify({"success": True, "message": f"Processamento concluído para {len(tasks)} tarefas", "results": results})
    except Exception as e:
        logging.exception("complete error")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
