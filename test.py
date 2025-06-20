import os
from datetime import datetime, timedelta
import openai
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# --- 設定項目 (ここを編集してください) ---

# 1. 要約したいチャンネル名のリスト
# (例: ['general', 'random', 'tech-talk'])
TARGET_CHANNEL_NAMES = ["要約したいチャンネル名"]
# 2. 要約を投稿したいチャンネルの「チャンネルID」
# ※チャンネル名はNG。IDはチャンネル名を右クリックして「リンクをコピー」で確認できます (例: C0123ABCDE)
SUMMARY_POST_CHANNEL_ID = "投稿したいチャンネルのリンク" 
# 3. 使用するAIモデル
# (高性能なモデルがおすすめ: gpt-4, gpt-4o, gpt-3.5-turboなど)
AI_MODEL = "gpt-4.1-nano"

# --- 設定はここまで ---

# .envファイルから環境変数を読み込む
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# APIクライアントの初期化
try:
    slack_client = WebClient(token=SLACK_BOT_TOKEN)
    openai.api_key = OPENAI_API_KEY
    print("apiは正常です")

except Exception as e:
    print(f"環境変数の設定にエラーがあります: {e}")
    exit()

def get_all_public_channels():
    """
    ワークスペースの全パブリックチャンネルの {名前: ID} の辞書を取得する
    """
    try:
        response = slack_client.conversations_list(types="public_channel")
        return {ch["name"]: ch["id"] for ch in response["channels"]}
    except SlackApiError as e:
        print(f"Slackチャンネルリストの取得に失敗しました: {e.response['error']}")
        return {}



def get_channel_messages(channel_id):
    """
    指定されたチャンネルの過去24時間のメッセージを取得する
    """
    try:
        # 24時間前のタイムスタンプを計算
        yesterday = datetime.now() - timedelta(days=1)
        oldest_timestamp = yesterday.timestamp()
        response = slack_client.conversations_history(
            channel=channel_id, oldest=oldest_timestamp
        )

        # メッセージを整形して返す (ユーザーIDとテキスト)
        formatted_messages = []
        for msg in response["messages"]:
            # スレッド内の返信は除外 (メインメッセージのみ対象)
            if "thread_ts" not in msg or msg["ts"] == msg["thread_ts"]:
                # ユーザーIDの代わりにユーザー名を取得（よりリッチにする場合）
                user_id = msg.get("user", "Bot")
                text = msg.get("text", "")
                formatted_messages.append(f"{user_id}: {text}")
        return "\n".join(formatted_messages)

    except SlackApiError as e:
        print(
            f"メッセージの取得に失敗しました (Channel: {channel_id}): {e.response['error']}"
        )
        return ""

def summarize_step1_create_digest(discussions):
    """
    【AI処理 1段階目】収集した発言からプレーンテキストのダイジェストを生成
    """
    print("AI処理(1/2): ダイジェストを生成中...")
    prompt = f"""
以下のSlackでの会話内容を、チャンネルごとに要約してください。
重要な議論、決定事項、面白いトピックなどを簡潔にまとめてください。
出力はチャンネル名を`#チャンネル名`の形式で記述してください。
--- 会話内容 ---
{discussions}
--- ここまで ---
今日のダイジェスト:
"""
    try:
        response = openai.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "あなたは優秀なアシスタントです。Slackの会話を要約し、ダイジェストを作成します。",
                },

                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"OpenAI API(1段階目)でエラーが発生しました: {e}")
        return "ダイジェストの生成に失敗しました。"

def summarize_step2_format_links(plain_summary, channel_map):
    """
    【AI処理 2段階目】プレーンテキストのダイジェストに正しいチャンネルリンクを埋め込む
    """
    print("AI処理(2/2): チャンネルリンクをフォーマット中...")

    # チャンネル名とIDのマッピング情報をプロンプトに追加
    channel_info = "\n".join(
        [f"- `#`{name}`:` `<#{id}|{name}>`" for name, id in channel_map.items()]
    )
    prompt = f"""

以下の「ダイジェスト文章」に含まれる`#チャンネル名`という部分を、
Slackでクリック可能なリンク形式 `<#チャンネルID|チャンネル名>` に書き換えてください。
チャンネル名とIDの対応は、以下の「チャンネル情報」を厳密に参照してください。
対応表にないチャンネル名は、そのままテキストとして残してください。
--- チャンネル情報 ---
{channel_info}
--- ここまで ---
--- ダイジェスト文章 ---
{plain_summary}
--- ここまで ---
フォーマット後のダイジェスト:
"""
    try:
        response = openai.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "あなたは文章のフォーマットが得意なアシスタントです。指示に従ってテキストを正確に書き換えます。",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,  # 創造性を抑え、指示に忠実に動かす
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"OpenAI API(2段階目)でエラーが発生しました: {e}")
        return "ダイジェストのフォーマットに失敗しました。"

def post_to_slack(channel_id, text):
    """
    指定されたチャンネルにメッセージを投稿する
    """
    try:
        slack_client.chat_postMessage(
            channel=channel_id,
            text=text,
            unfurl_links=False,  # リンクのプレビューを無効化
            unfurl_media=False,
        )

        print(f"チャンネル {channel_id} への投稿に成功しました。")
    except SlackApiError as e:
        print(f"Slackへの投稿に失敗しました: {e.response['error']}")

def main():
    """
    メイン処理
    """
    print("処理を開始します...")
    # 1. チャンネル名とIDの対応表を取得
    all_channels_map = get_all_public_channels()
    if not all_channels_map:
        return

    # 2. 対象チャンネルの会話を収集
    all_discussions = ""
    target_channel_map = {}  # リンク化に必要な対象チャンネルだけの対応表
    for channel_name in TARGET_CHANNEL_NAMES:
        if channel_name in all_channels_map:
            channel_id = all_channels_map[channel_name]
            target_channel_map[channel_name] = channel_id  # 対応表を作成
            print(f"チャンネル「{channel_name}」からメッセージを取得中...")
            messages = get_channel_messages(channel_id)
            if messages:
                all_discussions += (
                    f"\n--- チャンネル: #{channel_name} ---\n{messages}\n"
                )
        else:
            print(f"警告: チャンネル「{channel_name}」が見つかりませんでした。")

    if not all_discussions:
        print("過去24時間に収集対象のメッセージはありませんでした。")
        # メッセージがなくてもその旨を通知したい場合はここで投稿処理を呼ぶ
        # post_to_slack(SUMMARY_POST_CHANNEL_ID, "今日のダイジェスト: 過去24時間の特記事項はありませんでした。")
        return

    # 3. 【AI処理 1段階目】ダイジェストを生成
    plain_summary = summarize_step1_create_digest(all_discussions)
    # 4. 【AI処理 2段階目】チャンネルリンクをフォーマット
    formatted_summary = summarize_step2_format_links(plain_summary, target_channel_map)
    # 最終的な投稿メッセージを作成
    final_post_text = f"【AIデイリーダイジェスト :bell:】\n\n{formatted_summary}"
    # 5. Slackに投稿
    post_to_slack(SUMMARY_POST_CHANNEL_ID, final_post_text)
    print("すべての処理が完了しました。")

if __name__ == "__main__":
    main()
