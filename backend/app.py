# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, random, time, os, logging
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
logging.basicConfig(level=logging.INFO)

API_BASE_URL = "https://edusp-api.ip.tv"
# Use your frontend origin URL here (Render) if you want it in headers
CLIENT_ORIGIN = os.environ.get("CLIENT_ORIGIN", "https://servidorteste.vercel.app/")
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

# ---- helper: transform JSON / build answers (adapted from your JS) ----
def remove_html_tags(s):
    import re
    return re.sub('<[^<]+?>', '', s or '').strip()

def transform_json_for_submission(task_json):
    """
    Given a task JSON from /tms/task/{id}, build 'answers' payload matching original transformJson logic.
    This returns a dict: {"answers": {questionId: {...}} , "accessed_on":..., "executed_on":...}
    """
    if not task_json or "questions" not in task_json:
        raise ValueError("Estrutura inválida")
    novo = {
        "accessed_on": task_json.get("accessed_on"),
        "executed_on": task_json.get("executed_on"),
        "answers": {}
    }
    answers = {}
    for q in task_json.get("questions", []):
        qid = q.get("id")
        qtype = q.get("type")
        payload = {"question_id": qid, "question_type": qtype, "answer": None}
        try:
            if qtype == "order-sentences":
                if q.get("options", {}).get("sentences"):
                    payload["answer"] = [s.get("value") for s in q["options"]["sentences"]]
            elif qtype == "fill-words":
                phrase = q.get("options", {}).get("phrase", [])
                if phrase:
                    payload["answer"] = [item.get("value") for idx,item in enumerate(phrase) if idx % 2 == 1]
            elif qtype == "text_ai":
                payload["answer"] = {"0": remove_html_tags(q.get("comment") or "")}
            elif qtype == "fill-letters":
                if "options" in q and "answer" in q["options"]:
                    payload["answer"] = q["options"]["answer"]
            elif qtype == "cloud":
                if q.get("options", {}).get("ids"):
                    payload["answer"] = q["options"]["ids"]
            elif qtype == "multiple_choice":
                # try to find correct option, otherwise pick first
                opts = q.get("options", [])
                correct = [o for o in opts if o.get("correct")]
                if correct:
                    payload["answer"] = {str(correct[0].get("id")): True}
                elif opts:
                    payload["answer"] = {str(opts[0].get("id")): True}
                else:
                    payload["answer"] = {}
            else:
                # generic mapping for options as object
                opts = q.get("options")
                if isinstance(opts, dict):
                    out = {}
                    for k,v in opts.items():
                        if isinstance(v, dict):
                            out[k] = v.get("answer", False)
                        else:
                            out[k] = False
                    payload["answer"] = out
                else:
                    payload["answer"] = {}
        except Exception as e:
            logging.exception("Erro processando questão %s: %s", qid, e)
            payload["answer"] = {}
        answers[str(qid)] = payload
    novo["answers"] = answers
    return novo

# ---- AUTH ----
@app.route("/auth", methods=["POST"])
def auth():
    try:
        data = request.get_json(force=True)
        ra = data.get("ra")
        senha = data.get("password")
        if not ra or not senha:
            return jsonify({"success": False, "message": "RA e senha obrigatórios"}), 400
        payload = {"realm": "edusp", "platform": "webclient", "id": ra, "password": senha}
        r = requests.post(f"{API_BASE_URL}/registration/edusp", headers=default_headers(), json=payload, timeout=15)
        if r.status_code != 200:
            logging.warning("auth failed: %s %s", r.status_code, r.text[:300])
            return jsonify({"success": False, "message": "Falha no login", "detail": r.text}), r.status_code
        j = r.json()
        logging.info("DEBUG /auth login OK: ra=%s nick=%s", ra, j.get("nick"))
        # return both shapes for compatibility
        return jsonify({"success": True, "user_info": {"auth_token": j.get("auth_token"), "nick": j.get("nick")}, "auth_token": j.get("auth_token"), "nick": j.get("nick")})
    except Exception as e:
        logging.exception("auth error")
        return jsonify({"success": False, "message": str(e)}), 500

# ---- ROOMS helper ----
def fetch_rooms(token):
    r = requests.get(f"{API_BASE_URL}/room/user?list_all=true&with_cards=true", headers=default_headers({"x-api-key": token}), timeout=15)
    r.raise_for_status()
    return r.json()

# ---- TASKS ----
@app.route("/tasks", methods=["POST"])
def tasks():
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
            logging.info("DEBUG /tasks: nenhuma sala encontrada")
            return jsonify({"success": True, "tasks": [], "count": 0, "message": "Nenhuma sala encontrada"})

        base_params = {"limit":100,"offset":0,"is_exam":"false","with_answer":"true","is_essay":"false","with_apply_moment":"true"}
        if task_filter == "expired":
            base_params["expired_only"] = "true"; base_params["filter_expired"]="false"
        else:
            base_params["expired_only"] = "false"; base_params["filter_expired"]="true"

        tasks_found = []
        for target in list(targets):
            params = dict(base_params); params["publication_target"]=target
            try:
                r = requests.get(f"{API_BASE_URL}/tms/task/todo", params=params, headers=default_headers({"x-api-key": token}), timeout=15)
                if r.status_code == 200:
                    payload = r.json()
                    if isinstance(payload, list):
                        tasks_found.extend(payload)
                    elif isinstance(payload, dict) and "tasks" in payload:
                        tasks_found.extend(payload.get("tasks", []))
            except Exception:
                logging.exception("Erro ao buscar tasks para target %s", target)
                continue

        logging.info("DEBUG /tasks: filtro=%s total=%s", task_filter, len(tasks_found))
        return jsonify({"success": True, "tasks": tasks_found, "count": len(tasks_found)})
    except Exception as e:
        logging.exception("tasks error")
        return jsonify({"success": False, "message": str(e)}), 500

# compatibility endpoints:
@app.route("/tasks/pending", methods=["POST"])
def tasks_pending():
    return tasks()

@app.route("/tasks/expired", methods=["POST"])
def tasks_expired():
    return tasks()

# ---- PROCESS single task ----
def process_one_task(token, task_obj, time_min=1, time_max=3, is_draft=False):
    try:
        task_id = task_obj.get("id")
        if not task_id:
            return {"success": False, "message": "task sem id", "task_id": None}

        r = requests.get(f"{API_BASE_URL}/tms/task/{task_id}", headers=default_headers({"x-api-key": token}), timeout=15)
        r.raise_for_status()
        task_info = r.json()
        # build payload using transform_json_for_submission
        submission_payload = transform_json_for_submission(task_info)

        # Simulate processing time — minutes -> seconds
        sec_min = max(1, int(time_min)) * 60
        sec_max = max(1, int(time_max)) * 60
        processing_time = random.randint(sec_min, sec_max)
        logging.info("PROCESS task %s sleep %s", task_id, processing_time)
        time.sleep(processing_time)

        # The final request to submit answers — send x-api-key as token
        submit_url = f"{API_BASE_URL}/tms/task/{task_id}/answer"
        headers = default_headers({"x-api-key": token})
        logging.info("DEBUG submit headers: %s", headers)
        logging.info("DEBUG submit payload keys: %s", list(submission_payload.keys()))
        resp = requests.post(submit_url, headers=headers, json=submission_payload, timeout=30)
        resp.raise_for_status()
        logging.info("DEBUG submit resp ok for %s", task_id)
        return {"success": True, "message": "Enviado", "task_id": task_id, "result": resp.json()}
    except requests.HTTPError as he:
        logging.exception("HTTP error processing task %s", task_obj.get("id"))
        return {"success": False, "message": f"HTTP error: {he}", "task_id": task_obj.get("id")}
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
        res = process_one_task(token, task, time_min, time_max, is_draft)
        return jsonify(res)
    except Exception as e:
        logging.exception("task_process_route error")
        return jsonify({"success": False, "message": str(e)}), 500

# ---- COMPLETE multiple tasks (parallel) ----
@app.route("/complete", methods=["POST"])
def complete_route():
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
        max_workers = min(6, max(1, len(tasks)))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(process_one_task, token, t, time_min, time_max, is_draft) for t in tasks]
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
    return jsonify({"status":"ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
