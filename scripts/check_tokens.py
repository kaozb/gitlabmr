#!/usr/bin/env python3
"""
一键探测所有 GitLab 多仓库前缀令牌的有效性
用法: python scripts/check_tokens.py
"""

import sqlite3
import requests
import sys
import os
from datetime import datetime

# 数据库路径
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'gitlabmr.sqlite3')

# 复用服务端的 GitLab 地址归一化：api_url 若被填成项目页面地址
# （如 https://host/WAF），必须先归一到 scheme://host 再拼 /api/v4，
# 否则请求会被 GitLab 路由到网页登录页，拿到 HTML 当 JSON 解析报错。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from reviewer.conf import api_origin


def get_tokens_from_db():
    """从数据库读取所有启用的令牌"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 查询所有 enabled=1 的记录
    cursor.execute("""
        SELECT id, prefix, api_url, token, enabled
        FROM reviewer_repotokenentry
        WHERE enabled = 1
        ORDER BY prefix
    """)
    
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def check_token(token, api_url):
    """
    验证单个 GitLab 令牌是否有效
    返回: (is_valid, message, user_info)
    """
    try:
        # 构建 API 端点 - 使用 /api/v4/user 获取当前用户信息
        url = f"{api_origin(api_url)}/api/v4/user"
        
        headers = {
            'PRIVATE-TOKEN': token
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            user = response.json()
            return True, "有效", {
                'username': user.get('username', 'N/A'),
                'name': user.get('name', 'N/A'),
                'id': user.get('id', 'N/A')
            }
        elif response.status_code == 401:
            return False, "无效（401 Unauthorized）", None
        elif response.status_code == 403:
            return False, "无权限（403 Forbidden）", None
        else:
            return False, f"错误（HTTP {response.status_code}）", None
            
    except requests.exceptions.Timeout:
        return False, "超时（10秒）", None
    except requests.exceptions.ConnectionError as e:
        return False, f"连接失败: {str(e)}", None
    except Exception as e:
        return False, f"异常: {str(e)}", None


def main():
    print("=" * 80)
    print(f"GitLab 多仓库前缀令牌有效性探测")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    # 获取令牌列表
    try:
        tokens = get_tokens_from_db()
    except Exception as e:
        print(f"❌ 读取数据库失败: {e}")
        sys.exit(1)
    
    if not tokens:
        print("⚠️  没有找到启用的令牌（enabled=1）")
        sys.exit(0)
    
    print(f"📊 共发现 {len(tokens)} 个启用的令牌，开始探测...\n")
    
    results = []
    valid_count = 0
    invalid_count = 0
    
    for idx, item in enumerate(tokens, 1):
        prefix = item['prefix']
        api_url = item['api_url']
        token = item['token']
        
        print(f"[{idx}/{len(tokens)}] 前缀: {prefix}")
        print(f"       API URL: {api_url}")
        print(f"       Token: {token[:8]}...{token[-4:]}" if len(token) > 12 else f"       Token: {token}")
        
        is_valid, message, user_info = check_token(token, api_url)
        
        if is_valid:
            valid_count += 1
            status_icon = "✅"
            print(f"       结果: {status_icon} {message}")
            if user_info:
                print(f"       用户: {user_info['name']} (@{user_info['username']})")
        else:
            invalid_count += 1
            status_icon = "❌"
            print(f"       结果: {status_icon} {message}")
        
        results.append({
            'prefix': prefix,
            'api_url': api_url,
            'token': token,
            'valid': is_valid,
            'message': message,
            'user_info': user_info
        })
        
        print()  # 空行分隔
    
    # 汇总统计
    print("=" * 80)
    print("📈 探测结果汇总")
    print("=" * 80)
    print(f"总计: {len(tokens)} 个令牌")
    print(f"✅ 有效: {valid_count} 个")
    print(f"❌ 无效: {invalid_count} 个")
    print()
    
    # 显示无效令牌详情
    if invalid_count > 0:
        print("❌ 无效令牌列表:")
        for r in results:
            if not r['valid']:
                print(f"  - {r['prefix']}: {r['message']}")
        print()
    
    # 显示有效令牌详情
    if valid_count > 0:
        print("✅ 有效令牌列表:")
        for r in results:
            if r['valid']:
                user = r['user_info']
                print(f"  - {r['prefix']}: {user['name']} (@{user['username']})")
        print()
    
    # 导出结果到文件
    output_file = os.path.join(os.path.dirname(__file__), '..', 'token_check_results.txt')
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f"GitLab 令牌探测结果 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 80 + "\n\n")
        
        f.write(f"总计: {len(tokens)} 个令牌\n")
        f.write(f"✅ 有效: {valid_count} 个\n")
        f.write(f"❌ 无效: {invalid_count} 个\n\n")
        
        f.write("-" * 80 + "\n")
        f.write("详细结果:\n")
        f.write("-" * 80 + "\n\n")
        
        for r in results:
            f.write(f"前缀: {r['prefix']}\n")
            f.write(f"API URL: {r['api_url']}\n")
            f.write(f"Token: {r['token'][:4]}****{r['token'][-4:]}（已打码）\n")
            f.write(f"状态: {'✅ 有效' if r['valid'] else '❌ 无效'}\n")
            f.write(f"详情: {r['message']}\n")
            if r['user_info']:
                f.write(f"用户: {r['user_info']['name']} (@{r['user_info']['username']})\n")
            f.write("\n")
    
    print(f"💾 详细结果已保存到: {output_file}")
    
    # 返回退出码：有无效令牌返回 1，全部有效返回 0
    sys.exit(1 if invalid_count > 0 else 0)


if __name__ == '__main__':
    main()
