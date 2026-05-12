import warnings
import pandas as pd
import json
import re
import os

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")


# 转换日期格式：6月22日 → 2025-06-22
def convert_date(chinese_date, file_name):
    match = re.match(r'(\d{1,2})月(\d{1,2})日', str(chinese_date))
    count = extract_number(file_name)
    if match:
        month, day = match.groups()
        if count:
            return f"{1998 + count}-{int(month):02d}-{int(day):02d}"
        else:
            return f"{pd.Timestamp.now().year}-{int(month):02d}-{int(day):02d}"
    return None


# 提取时长中的数字：104分钟 → 104；若无数字则为 NaN
def extract_duration(duration_str):
    match = re.search(r'\d+', str(duration_str))
    return int(match.group()) if match else pd.NA


# 中文数字到阿拉伯数字的简单映射
chinese_digit_map = {
    '零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10
}


def chinese_to_int(chinese_str):
    """把简单的中文数字（如 '十二', '二十', '二十三'）转成整数"""
    num = 0
    if chinese_str == '十':
        return 10
    if '十' in chinese_str:
        parts = chinese_str.split('十')
        if parts[0] == '':
            num += 10
        else:
            num += chinese_digit_map.get(parts[0], 0) * 10
        if parts[1] != '':
            num += chinese_digit_map.get(parts[1], 0)
    else:
        num += chinese_digit_map.get(chinese_str, 0)
    return num


def extract_number(s):
    match = re.search(r'第(.*?)届', s)
    if not match:
        return None
    content = match.group(1)
    if content.isdigit():
        return int(content)
    else:
        return chinese_to_int(content)


# 列名别名映射：规范化Excel中可能出现的列名变体
COLUMN_ALIASES = {
    '单元': ['单元', '电影单元', '所属单元', '展映单元'],
    '中文片名': ['中文片名', '片名', '影片名称', '电影名称', '名称', '影片名', '电影'],
    '日期': ['日期', '放映日期', '场次日期', '排片日期'],
    '放映时间': ['放映时间', '时间', '开场时间', '开始时间', '放映开始时间'],
    '时长': ['时长', '片长', '长度', '放映时长', '影片时长'],
    '导演': ['导演', '导演姓名', '执导'],
    '影院': ['影院', '电影院', '影院名称', '放映影院', '影城', '放映影院名称'],
    '影院地址': ['影院地址', '地址', '放映地址', '影城地址', '影院详细地址'],
}


def normalize_column(col_name):
    """根据别名表规范化列名"""
    col_str = str(col_name).strip().replace('　', ' ').replace('\xa0', ' ')
    for canonical, aliases in COLUMN_ALIASES.items():
        if col_str == canonical or col_str in aliases:
            return canonical
    return col_str


def extract_data(file_name):
    """读取Excel排片表，解析为films.json"""
    # 读取 Excel，跳过第一行表头，第二行为列名
    df = pd.read_excel(file_name, header=1)

    # 规范化列名（处理可能的别名）
    df.columns = [normalize_column(c) for c in df.columns]

    # 重命名列以匹配需求
    df = df.rename(columns={
        '单元': 'unit',
        '中文片名': 'title',
        '日期': 'date',
        '放映时间': 'start_time',
        '时长': 'duration',
        '导演': 'director',
        '影院': 'cinema_name',
        '影院地址': 'cinema_address',
    })

    # 检查必填列
    required_cols = ['title', 'date', 'start_time', 'duration', 'cinema_name']
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Excel中缺少必要列: {', '.join(missing)}，当前列名: {list(df.columns)}")

    # 可选列填默认值
    optional_defaults = {
        'cinema_address': '',
        'director': '无导演',
        'unit': '',
    }
    for col, default in optional_defaults.items():
        if col not in df.columns:
            df[col] = default

    df['date'] = df['date'].apply(convert_date, file_name=file_name)

    df['duration'] = df['duration'].apply(extract_duration).astype('Int64')

    # 导演列为空则赋值 无导演
    df['director'] = df['director'].fillna("无导演")

    # 删除 duration 为 NaN 的行
    df = df[df['duration'].notna()]

    # 清理影院地址中的换行符、空格等
    df['cinema_address'] = df['cinema_address'].astype(str).str.strip()

    # 保留字段顺序并添加 ID
    df = df[['title', 'date', 'start_time', 'duration', 'cinema_name',
             'cinema_address', 'director', 'unit']]
    df.insert(0, 'id', range(1, len(df) + 1))

    # 转换为 JSON，确保 duration 是 Python int
    records = []
    for row in df.to_dict(orient='records'):
        row['duration'] = int(row['duration'])
        records.append(row)

    # 保存为 JSON 文件（使用绝对路径，与 app.py 的 resource_path 对齐）
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'films.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    return records


if __name__ == '__main__':
    extract_data('第27届上海国际电影节排片表.xlsx')
