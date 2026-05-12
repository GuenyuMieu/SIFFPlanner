import json
import time
import threading
import math
from flask import Flask, render_template, request, jsonify
import requests
import os
import sys

app = Flask(__name__)

# ======== 本地配置文件（用户通过网页填写保存） ========
CONFIG_FILE = os.path.join(os.path.abspath("."), "amap_config.json")

def load_amap_config():
    """从本地 JSON 读取高德 Key 配置"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"amap_key": "", "amap_jsapi_key": "", "amap_security_code": ""}

def save_amap_config(data):
    """保存高德 Key 配置到本地 JSON"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 启动时加载配置
_config = load_amap_config()
AMAP_KEY = _config.get("amap_key", "")           # Web服务 Key（后端 REST API 用）
AMAP_JSAPI_KEY = _config.get("amap_jsapi_key", "")  # Web端 Key（前端地图显示用）
AMAP_SECURITY_CODE = _config.get("amap_security_code", "")  # Web端 安全密钥
GEOCODE_INTERVAL = 0.35     # 每 0.35 秒最多一次地理编码请求，保证 QPS <= 3
# ================================

# 获取打包兼容的路径
def resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

# 辅助：把 "HH:MM" 转为分钟数
def hhmm_to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m

# 构建去重影片列表：以 (title, director) 为唯一键
def build_unique_movies(raw_movies):
    groups = {}
    for item in raw_movies:
        key = f"{item['title']}||{item.get('director', '')}"
        if key not in groups:
            groups[key] = {
                "key": key,
                "title": item["title"],
                "director": item.get("director", ""),
                "unit": item.get("unit", ""),
                "cinemas": [],
                "_cinema_set": set(),
                "screening_count": 0,
                "screenings": [],
                "dates": [],
                "_date_set": set(),
            }
        g = groups[key]
        if item["cinema_name"] not in g["_cinema_set"]:
            g["_cinema_set"].add(item["cinema_name"])
            g["cinemas"].append(item["cinema_name"])
        g["screenings"].append({
            "id": item["id"],
            "date": item["date"],
            "start_time": item["start_time"],
            "duration": item["duration"],
            "cinema_name": item["cinema_name"],
            "cinema_address": item.get("cinema_address", ""),
        })
        if item["date"] not in g["_date_set"]:
            g["_date_set"].add(item["date"])
            g["dates"].append(item["date"])
        g["screening_count"] += 1

    result = []
    for g in groups.values():
        g["dates"].sort()
        g["cinemas"].sort()
        del g["_cinema_set"]
        del g["_date_set"]
        result.append(g)
    result.sort(key=lambda x: x["title"])
    return result

# 读取并解析本地电影排期数据
with open(resource_path("films.json"), "r", encoding="utf-8") as f:
    raw_movies = json.load(f)

# --- 预先构建：每个 cinema_name 对应的地址（字符串） ---
cinema_address_map = {}
for item in raw_movies:
    cinema_address_map[item["cinema_name"]] = item["cinema_address"]


def geocode_address(address: str):
    """
    调用高德地理编码，并将结果缓存。每次请求前按 GEOCODE_INTERVAL 等待，保证 QPS 不超限。
    如果高德 Key 未配置，直接返回 None，避免无效 HTTP 请求。
    """
    global _last_geocode_time

    if address in geocode_cache:
        return geocode_cache[address]

    if not AMAP_KEY:
        lng, lat = None, None
        geocode_cache[address] = (lng, lat)
        return lng, lat

    with _geocode_lock:
        elapsed = time.time() - _last_geocode_time
        if elapsed < GEOCODE_INTERVAL:
            time.sleep(GEOCODE_INTERVAL - elapsed)

        url = "https://restapi.amap.com/v3/geocode/geo"
        params = {
            "address": address,
            "key": AMAP_KEY,
            "output": "JSON"
        }
        try:
            r = requests.get(url, params=params, timeout=5)
            j = r.json()
        except Exception as e:
            print(f"[ERROR] 地理编码异常 for '{address}': {e}")
            lng, lat = None, None
        else:
            if j.get("status") == "1" and j.get("geocodes"):
                loc = j["geocodes"][0]["location"]
                try:
                    lng, lat = map(float, loc.split(","))
                except:
                    lng, lat = None, None
            else:
                lng, lat = None, None

        _last_geocode_time = time.time()
        geocode_cache[address] = (lng, lat)
        return lng, lat

def preload_all_geocodes():
    """
    后台线程预加载所有影院地址，避免后续 QPS 达到上限时再逐条获取。
    如果高德 Key 未配置，直接跳过。
    """
    if not AMAP_KEY:
        return
    for addr in all_addresses:
        geocode_address(addr)

# ======== 数据加载和预处理函数 ========
def load_and_process_data():
    global raw_movies, cinema_address_map, movies_by_date, timeline_data, all_addresses, unique_movies

    # 读取 JSON
    with open(resource_path("films.json"), "r", encoding="utf-8") as f:
        raw_movies = json.load(f)

    # 构建影院地址映射
    cinema_address_map = {item["cinema_name"]: item["cinema_address"] for item in raw_movies}

    # 按日期分组 + timeline 计算
    movies_by_date.clear()
    for item in raw_movies:
        d = item["date"]
        if d not in movies_by_date:
            movies_by_date[d] = []
        start_min = hhmm_to_minutes(item["start_time"])
        end_min = start_min + int(item["duration"])
        movies_by_date[d].append({
            "id": item["id"],
            "title": item["title"],
            "start_time": item["start_time"],
            "duration": int(item["duration"]),
            "end_time": f"{end_min//60:02d}:{end_min%60:02d}",
            "cinema_name": item["cinema_name"],
            "cinema_address": item["cinema_address"],
            "director": item["director"],
            "unit": item["unit"],
            "start_min": start_min,
            "end_min": end_min
        })

    timeline_data.clear()
    for d, arr in movies_by_date.items():
        min_start = min(m["start_min"] for m in arr)
        max_end   = max(m["end_min"]   for m in arr)
        span = max_end - min_start or 1
        processed = []
        for m in arr:
            offset_pct = (m["start_min"] - min_start) / span * 100
            width_pct  = (m["end_min"]   - m["start_min"]) / span * 100
            processed.append({
                "id": m["id"],
                "title": m["title"],
                "start_time": m["start_time"],
                "end_time": m["end_time"],
                "duration": m["duration"],
                "cinema_name": m["cinema_name"],
                "cinema_address": m["cinema_address"],
                "director": m["director"],
                "unit": m["unit"],
                "offset_pct": offset_pct,
                "width_pct": width_pct
            })
        processed.sort(key=lambda x: hhmm_to_minutes(x["start_time"]))
        timeline_data[d] = {
            "min_time": f"{min_start//60:02d}:{min_start%60:02d}",
            "max_time": f"{max_end//60:02d}:{max_end%60:02d}",
            "items": processed
        }

    # 所有地址
    all_addresses = set(item["cinema_address"] for item in raw_movies)
    # print(all_addresses)

    # 预加载地理编码
    threading.Thread(target=preload_all_geocodes, daemon=True).start()

    # 构建去重影片列表
    unique_movies = build_unique_movies(raw_movies)


# 初始化全局变量
raw_movies = []
cinema_address_map = {}
movies_by_date = {}
timeline_data = {}
all_addresses = set()
unique_movies = []
geocode_cache = {}
_last_geocode_time = 0.0
_geocode_lock = threading.Lock()

load_and_process_data()


# threading.Thread(target=preload_all_geocodes, daemon=True).start()
# ================================

UPLOAD_FOLDER = "./uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
SOURCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "current_source.txt")

def safe_filename_static(filename):
    return re.sub(r'[\\/*?:"<>|]', "_", filename)

def get_source_name():
    """获取当前使用的Excel文件名，默认扫描uploads下最新的上海国际电影节文件"""
    try:
        with open(SOURCE_FILE, "r", encoding="utf-8") as f:
            name = f.read().strip()
            if name and os.path.exists(os.path.join(UPLOAD_FOLDER, safe_filename_static(name))):
                return name
    except:
        pass
    # 扫描uploads目录，找名字含"上海国际电影节"的最新文件
    try:
        festival_files = []
        for fn in os.listdir(UPLOAD_FOLDER):
            if "上海国际电影节" in fn and fn.rsplit(".", 1)[-1].lower() in {"xls", "xlsx"}:
                fp = os.path.join(UPLOAD_FOLDER, fn)
                festival_files.append((os.path.getmtime(fp), fn))
        if festival_files:
            festival_files.sort(reverse=True)
            return festival_files[0][1]
    except:
        pass
    return ""

@app.route("/")
def index():
    return render_template("index.html",
                           timeline_data=timeline_data,
                           unique_movies=unique_movies,
                           source_name=get_source_name(),
                           amap_key=AMAP_JSAPI_KEY,
                           amap_security_code=AMAP_SECURITY_CODE,
                           amap_configured=bool(AMAP_KEY and AMAP_JSAPI_KEY))

@app.route("/route_info", methods=["POST"])
def route_info():
    """
    1. 接收 selected_movies，排序后按“连续去重影院”；
    2. 从缓存读取坐标（或二次 geocode）；
    3. 调用 v4 骑行接口获取 distance（米）、duration（秒）→
       换算成 distance_km（公里，一位小数）、bike_duration_min（分钟）；
       并调用 v3 公交换乘接口获取 transit_duration_min；
    4. 返回 points 与 distances。其中 points 中附带：电影名称、开始/结束时间；
    """
    data = request.get_json()
    selected = data.get("selected_movies", [])
    selected.sort(key=lambda x: (x["date"], hhmm_to_minutes(x["start_time"])))

    # --- 修改后：连续相同影院的场次合并，并把 movie_titles 存成列表 ---
    cinemas = []
    previous_addr = None
    for m in selected:
        addr = m["cinema_address"]
        if addr != previous_addr:
            # 第一次遇到这个影院，创建一个新的条目，把当前 title 放入 movie_titles 列表
            cinemas.append({
                "cinema_name": m["cinema_name"],
                "cinema_address": m["cinema_address"],
                "movie_titles": [m["title"]],  # 用列表保存所有连续场次的标题
                "movie_start": m["start_time"],
                "movie_end": m["end_time"]
            })
        else:
            # 连续相同影院：追加新的 title，并更新该影院条目的结束时间
            cinemas[-1]["movie_titles"].append(m["title"])
            cinemas[-1]["movie_end"] = m["end_time"]
        previous_addr = addr

    # 获取坐标，并组装 points 列表
    points = []
    for c in cinemas:
        addr = c["cinema_address"]
        lng, lat = geocode_cache.get(addr, (None, None))
        if lng is None and lat is None:
            lng, lat = geocode_address(addr)
        points.append({
            "cinema_name": c["cinema_name"],
            "address": addr,
            # 将 movie_titles 列表拼成一个字符串，前端显示时就能看到“片名1 / 片名2”
            "movie_title": " / ".join(c["movie_titles"]),
            "movie_start": c["movie_start"],
            "movie_end": c["movie_end"],
            "lng": lng,
            "lat": lat
        })

    # 计算每段骑行/公交信息
    distances = []
    for i in range(len(points) - 1):
        p1 = points[i]
        p2 = points[i + 1]
        if p1["lng"] is None or p2["lng"] is None:
            distances.append({
                "from_index": i,
                "to_index":   i + 1,
                "distance_km": None,
                "bike_duration_min": None,
                "transit_duration_min": None
            })
            continue

        if p1["lng"] == p2["lng"] and p1["lat"] == p2["lat"]:
            distances.append({
                "from_index": i,
                "to_index":   i + 1,
                "distance_km": 0.0,
                "bike_duration_min": 0,
                "transit_duration_min": 0
            })
            continue

        # ---- 骑行 v4 接口 ----
        url_bike = "https://restapi.amap.com/v4/direction/bicycling"
        params_bike = {
            "origin":      f"{p1['lng']:.6f},{p1['lat']:.6f}",
            "destination": f"{p2['lng']:.6f},{p2['lat']:.6f}",
            "key":         AMAP_KEY
        }
        try:
            r_bike = requests.get(url_bike, params=params_bike, timeout=5)
            j_bike = r_bike.json()
        except Exception as e:
            j_bike = {}
            print(f"[ERROR] v4 骑行接口异常：{e}")

        errcode = j_bike.get("errcode")
        paths = j_bike.get("data", {}).get("paths") if j_bike.get("data") else None

        dist_km = None
        bike_min = None
        transit_min = None

        if errcode == 0 and isinstance(paths, list) and len(paths) > 0:
            path0 = paths[0]
            dist_m = path0.get("distance", 0)
            dur_s  = path0.get("duration", 0)
            dist_km = round(dist_m / 1000.0, 1)
            try:
                bike_min = int(round(int(dur_s) / 60))
            except:
                bike_min = None

        # ---- 公交换乘接口 ----
        city = "上海"
        if "市" in p1["address"]:
            city = p1["address"].split("市")[0]
        if city:
            url_transit = "https://restapi.amap.com/v3/direction/transit/integrated"
            params_transit = {
                "origin":      f"{p1['lng']:.6f},{p1['lat']:.6f}",
                "destination": f"{p2['lng']:.6f},{p2['lat']:.6f}",
                "city":        city,
                "key":         AMAP_KEY,
                "extensions":  "base"
            }
            try:
                r_transit = requests.get(url_transit, params=params_transit, timeout=5)
                j_transit = r_transit.json()
            except Exception as e2:
                j_transit = {}
                print(f"[ERROR] 公交换乘接口异常：{e2}")

            if j_transit.get("status") == "1" and j_transit.get("route", {}).get("transits"):
                dur_s2 = j_transit["route"]["transits"][0].get("duration", 0)
                try:
                    transit_min = int(round(int(dur_s2) / 60))
                except:
                    transit_min = None

        distances.append({
            "from_index": i,
            "to_index":   i + 1,
            "distance_km": dist_km,
            "bike_duration_min": bike_min,
            "transit_duration_min": transit_min
        })

    return jsonify({
        "points": points,
        "distances": distances
    })


# 上传文件后刷新后台数据
from convert import extract_data
import re

ALLOWED_EXTENSIONS = {"xls", "xlsx"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def safe_filename(filename):
    # 只去掉路径分隔符 / \ 和冒号等特殊字符
    return re.sub(r'[\\/*?:"<>|]', "_", filename)

@app.route("/upload_excel", methods=["POST"])
def upload_excel():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "No selected file"}), 400

    if file and allowed_file(file.filename):
        if "上海国际电影节" not in file.filename:
            return jsonify({"status": "error", "message": '文件名需包含“上海国际电影节”，请确认上传的是正确的排片表'}), 400

        filename = safe_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

        try:
            with open(SOURCE_FILE, "w", encoding="utf-8") as f:
                f.write(file.filename)

            extract_data(filepath)
            load_and_process_data()

            return jsonify({"status": "ok", "message": f"已加载：{file.filename}"})
        except Exception as e:
            return jsonify({"status": "error", "message": f'解析失败：{str(e)}。请确认Excel排片表格式正确（包含：中文片名、日期、放映时间、时长、导演、影院等列）'}), 500
    else:
        return jsonify({"status": "error", "message": "文件格式不支持，请上传 .xls 或 .xlsx 文件"}), 400

# ======== Excel 文件管理 ========

@app.route("/api/excel_files")
def excel_files():
    """列出uploads下所有Excel文件"""
    files = []
    try:
        for fn in os.listdir(UPLOAD_FOLDER):
            if fn.rsplit(".", 1)[-1].lower() in ALLOWED_EXTENSIONS:
                fp = os.path.join(UPLOAD_FOLDER, fn)
                files.append({"name": fn, "mtime": os.path.getmtime(fp)})
        files.sort(key=lambda x: x["mtime"], reverse=True)
    except:
        pass
    current = get_source_name()
    return jsonify({"files": files, "current": current})


@app.route("/api/select_excel", methods=["POST"])
def select_excel():
    """切换使用指定的Excel文件"""
    data = request.get_json()
    name = data.get("name", "")
    if not name or ".." in name or "/" in name or "\\" in name:
        return jsonify({"status": "error", "message": "无效的文件名"}), 400
    filepath = os.path.join(UPLOAD_FOLDER, name)
    if not os.path.exists(filepath):
        return jsonify({"status": "error", "message": "文件不存在"}), 404
    try:
        extract_data(filepath)
        load_and_process_data()
        with open(SOURCE_FILE, "w", encoding="utf-8") as f:
            f.write(name)
        return jsonify({"status": "ok", "message": f"已切换至：{name}"})
    except Exception as e:
        return jsonify({"status": "error", "message": f'解析失败：{str(e)}'}), 500


@app.route("/api/delete_excel", methods=["POST"])
def delete_excel():
    """删除指定的Excel文件"""
    data = request.get_json()
    name = data.get("name", "")
    if not name or ".." in name or "/" in name or "\\" in name:
        return jsonify({"status": "error", "message": "无效的文件名"}), 400
    filepath = os.path.join(UPLOAD_FOLDER, name)
    if not os.path.exists(filepath):
        return jsonify({"status": "error", "message": "文件不存在"}), 404
    try:
        # 在删除前先判断是否为当前使用的文件
        was_current = False
        if os.path.exists(SOURCE_FILE):
            try:
                with open(SOURCE_FILE, "r", encoding="utf-8") as f:
                    was_current = (f.read().strip() == name)
            except:
                pass

        os.remove(filepath)

        # 清除 source 记录（如果指向被删文件）
        if was_current and os.path.exists(SOURCE_FILE):
            try:
                os.remove(SOURCE_FILE)
            except:
                pass

        # 扫描是否还有剩余文件
        remaining = []
        for fn in os.listdir(UPLOAD_FOLDER):
            if fn.rsplit(".", 1)[-1].lower() in ALLOWED_EXTENSIONS:
                remaining.append(fn)

        if not remaining:
            # 没有文件了，清空数据
            with open(resource_path("films.json"), "w", encoding="utf-8") as f:
                json.dump([], f)
            load_and_process_data()
            return jsonify({"status": "ok", "message": f"已删除 {name}，无剩余排片文件", "reload": True})

        if not was_current:
            # 删除的不是当前文件，不需要重载数据
            return jsonify({"status": "ok", "message": f"已删除 {name}", "reload": False})

        # 当前文件被删了，切换到另一个
        # 优先选择包含「上海国际电影节」的文件
        new_current = None
        for fn in remaining:
            if "上海国际电影节" in fn:
                new_current = fn
                break
        if not new_current:
            new_current = remaining[0]

        new_path = os.path.join(UPLOAD_FOLDER, safe_filename_static(new_current))
        extract_data(new_path)
        load_and_process_data()
        with open(SOURCE_FILE, "w", encoding="utf-8") as f:
            f.write(new_current)
        return jsonify({"status": "ok", "message": f"已删除 {name}，自动切换至 {new_current}", "reload": True})
    except Exception as e:
        return jsonify({"status": "error", "message": f"删除失败：{str(e)}"}), 500


# ======== 新增：按距离筛选影院 ========
def haversine(lon1, lat1, lon2, lat2):
    """
    计算两点（经纬度）距离，返回单位：公里
    """
    # 将十进制度数转化为弧度
    lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat/2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    r = 6371.0  # 地球平均半径（km）
    return c * r

@app.route("/filter_cinemas", methods=["POST"])
def filter_cinemas():
    """
    接收 JSON：{ center_cinema: str, radius_km: float }
    返回 JSON：{ cinemas: [符合距离条件的cinema_name列表] }
    """
    data = request.get_json()
    center_name = data.get("center_cinema")
    try:
        radius_km = float(data.get("radius_km", 0))
    except:
        radius_km = 0

    # 验证输入
    if center_name not in cinema_address_map:
        return jsonify({"cinemas": []})

    # 拿到中心影院地址，先尝试从缓存取经纬度
    center_addr = cinema_address_map[center_name]
    lng_center, lat_center = geocode_cache.get(center_addr, (None, None))
    if lng_center is None or lat_center is None:
        lng_center, lat_center = geocode_address(center_addr)

    # 如果还是 None，就返回仅包含自己
    if lng_center is None or lat_center is None:
        return jsonify({"cinemas": [center_name]})

    # 遍历所有影院，计算距离
    result = []
    for name, addr in cinema_address_map.items():
        lng, lat = geocode_cache.get(addr, (None, None))
        if lng is None or lat is None:
            lng, lat = geocode_address(addr)
        if lng is None or lat is None:
            continue
        dist = haversine(lng_center, lat_center, lng, lat)
        if dist <= radius_km + 1e-6:  # 考虑浮点误差
            result.append(name)

    return jsonify({"cinemas": result})


# ======== 高德 Key 配置接口 ========
@app.route("/api/config", methods=["GET"])
def get_config():
    """返回当前配置"""
    cfg = load_amap_config()
    return jsonify({
        "configured": bool(cfg.get("amap_key") and cfg.get("amap_jsapi_key")),
        "config": cfg
    })

@app.route("/api/config", methods=["POST"])
def update_config():
    """保存用户提交的高德 Key 配置"""
    global AMAP_KEY, AMAP_JSAPI_KEY, AMAP_SECURITY_CODE
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "无效的请求数据"}), 400
    # 只提取三个合法字段
    cfg = {
        "amap_key": data.get("amap_key", "").strip(),
        "amap_jsapi_key": data.get("amap_jsapi_key", "").strip(),
        "amap_security_code": data.get("amap_security_code", "").strip(),
    }
    save_amap_config(cfg)
    # 同步更新全局变量
    AMAP_KEY = cfg["amap_key"]
    AMAP_JSAPI_KEY = cfg["amap_jsapi_key"]
    AMAP_SECURITY_CODE = cfg["amap_security_code"]
    return jsonify({"status": "ok", "message": "配置已保存，请刷新页面生效"})


# ======== 影片优先模式 API ========

@app.route("/api/movies")
def api_movies():
    """返回所有去重影片信息"""
    enriched = []
    for movie in unique_movies:
        enriched.append({
            "key": movie["key"],
            "title": movie["title"],
            "director": movie["director"],
            "unit": movie["unit"],
            "cinemas": movie["cinemas"],
            "screening_count": movie["screening_count"],
            "dates": movie["dates"],
        })
    return jsonify({"movies": enriched})


@app.route("/api/timeline_for_movies", methods=["POST"])
def timeline_for_movies():
    """
    接收 { movie_keys: ["title||director", ...] }
    返回跨日期筛选后的 timeline_data（仅包含感兴趣影片的场次）
    """
    data = request.get_json()
    movie_keys = set(data.get("movie_keys", []))

    # 收集所有感兴趣影片的 screening id
    interested_ids = set()
    for movie in unique_movies:
        if movie["key"] in movie_keys:
            for s in movie["screenings"]:
                interested_ids.add(s["id"])

    # 按日期构建筛选后的 timeline
    filtered = {}
    for d, arr in movies_by_date.items():
        filtered_items = [m for m in arr if m["id"] in interested_ids]
        if not filtered_items:
            continue

        min_start = min(m["start_min"] for m in filtered_items)
        max_end = max(m["end_min"] for m in filtered_items)
        span = max_end - min_start or 1

        processed = []
        for m in filtered_items:
            offset_pct = (m["start_min"] - min_start) / span * 100
            width_pct = (m["end_min"] - m["start_min"]) / span * 100
            processed.append({
                "id": m["id"],
                "title": m["title"],
                "start_time": m["start_time"],
                "end_time": m["end_time"],
                "duration": m["duration"],
                "cinema_name": m["cinema_name"],
                "cinema_address": m["cinema_address"],
                "director": m["director"],
                "unit": m["unit"],
                "offset_pct": offset_pct,
                "width_pct": width_pct,
            })
        processed.sort(key=lambda x: hhmm_to_minutes(x["start_time"]))
        filtered[d] = {
            "min_time": f"{min_start // 60:02d}:{min_start % 60:02d}",
            "max_time": f"{max_end // 60:02d}:{max_end % 60:02d}",
            "items": processed,
        }

    dates = sorted(filtered.keys())
    return jsonify({"dates": dates, "timeline_data": filtered})


# 启动时检查：如果 films.json 为空，尝试从 uploads 自动加载
if not raw_movies:
    src = get_source_name()
    if src:
        filepath = os.path.join(UPLOAD_FOLDER, safe_filename_static(src))
        if os.path.exists(filepath):
            try:
                extract_data(filepath)
                load_and_process_data()
                print(f"[INIT] 自动加载排片表：{src}")
            except Exception as e:
                print(f"[INIT] 自动加载失败：{e}")


if __name__ == "__main__":

    app.run(host="0.0.0.0", port=5000, debug=False)
