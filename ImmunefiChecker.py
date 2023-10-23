import requests
import re
import time
import os
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient

# Load environment variables from .env file
load_dotenv()

# Use the loaded environment variables
user_agent = os.getenv("USER_AGENT")
mongo_uri = os.getenv("MONGO_URI")
mongo_db = os.getenv("MONGO_DB")
telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")


def fetch_source_code(url):
    headers = {
        "User-Agent": user_agent,
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.text
    else:
        response.raise_for_status()

def extract_token_from_source(source):
    pattern = r"/_next/static/([^/]+)/_buildManifest\.js"
    match = re.search(pattern, source)
    if match:
        return match.group(1)
    return None

def fetch_data_using_token(token):
    data_url = f"https://immunefi.com/_next/data/{token}/explore.json"
    response = requests.get(data_url)
    if response.status_code == 200:
        return response.json()
    else:
        response.raise_for_status()

def connect_to_database():
    client = MongoClient(mongo_uri, tlsAllowInvalidCertificates=True)
    db = client[mongo_db]  # Database name
    return db

def insert_into_diff_table(differences, db):
    diff_collection = db["differences"]
    for diff in differences:
        diff_collection.update_one(
            {"project": diff["project"]},
            {
                "$set": {
                    "id": diff["id"],
                    "existing_updatedDate": diff["existing_updatedDate"],
                    "new_updatedDate": diff["new_updatedDate"],
                    "link_diff": diff["link_diff"]
                }
            },
            upsert=True  # Inserts if not exists, otherwise updates
        )

def compare_with_existing_data(new_data, db):
    bounties_collection = db["bounties"]
    existing_data_list = list(bounties_collection.find())
    existing_data = {item["project"]: {"updatedDate": item["updatedDate"], "assetLinks": item.get("assetLinks", [])} for item in existing_data_list}
    differences = []

    for item in new_data:
        project_id = item["id"]
        project_name = item["project"]
        updated_date = item["updatedDate"]
        asset_links = fetch_asset_links_for_bounty(project_id)

        if project_name in existing_data:
            
            # Print differences in UpdatedDate and AssetLinks (if any)
            if existing_data[project_name]["updatedDate"] != updated_date:
                print(f"UpdatedDate different for {project_name}: Old - {existing_data[project_name]['updatedDate']} | New - {updated_date}")

            if existing_data[project_name]["assetLinks"] != asset_links:
                print(f"AssetLinks different for {project_name}")

            # Continue with your comparison logic
            if (
                existing_data[project_name]["updatedDate"] != updated_date or
                existing_data[project_name]["assetLinks"] != asset_links
            ):
                link_diff = list(set(asset_links) - set(existing_data[project_name]["assetLinks"]))
                differences.append(
                    {
                        "id": project_id,
                        "project": project_name,
                        "existing_updatedDate": existing_data[project_name]["updatedDate"],
                        "new_updatedDate": updated_date,
                        "link_diff": link_diff
                    }
                )

    return differences

def update_bounties_table(updated_data, db):
    bounties_collection = db["bounties"]
    for data in updated_data:
        print(f"Updating bounties table for {data['project']}: New UpdatedDate - {data['new_updatedDate']} | AssetLinks - {fetch_asset_links_for_bounty(data['id'])}")
        bounties_collection.update_one(
            {"project": data["project"]},
            {"$set": {"updatedDate": data["new_updatedDate"], "assetLinks": fetch_asset_links_for_bounty(data["id"])}}
        )

def send_message_to_telegram(text):
    bot_token = telegram_bot_token
    chat_id = telegram_chat_id
    base_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    response = requests.post(base_url, data=payload)
    return response.json()

def initialize_bounties_table_if_empty(bounties, db):
    bounties_collection = db["bounties"]
    
    # Check if bounties collection is empty
    if bounties_collection.count_documents({}) == 0:
        for bounty in bounties:
            print("Initializing bounty:", bounty)
            bounty_id = bounty["id"]
            bounty["assetLinks"] = fetch_asset_links_for_bounty(bounty_id)
            
        bounties_collection.insert_many(bounties)

def fetch_asset_links_for_bounty(bounty_id):
    url = f"https://immunefi.com/bounty/{bounty_id}/"
    response = requests.get(url)

    if response.status_code == 200:
        content = response.text
        soup = BeautifulSoup(content, 'lxml')

        # Extract all <a> tags with a href attribute
        all_links = soup.find_all('a', href=True)

        # Filter out links containing 'github.com'
        github_links = [link['href'] for link in all_links if 'github.com' in link['href']]
        
        # Filter out links containing 'etherscan.io'
        etherscan_links = [link['href'] for link in all_links if 'etherscan.io' in link['href']]
        
        # Filter out links containing 'testnet.bscscan.com'
        testnet_bsc_links = [link['href'] for link in all_links if 'bscscan.com' in link['href']]

        # Combine all lists
        asset_links = github_links + etherscan_links + testnet_bsc_links
        
        return asset_links
    else:
        print(f"Failed to retrieve the content. HTTP status code: {response.status_code}")
        return []


while True:
    url = "https://immunefi.com/explore/"
    source_code = fetch_source_code(url)
    token = extract_token_from_source(source_code)
    data = fetch_data_using_token(token)
    bounties = data["pageProps"]["bounties"]

    db = connect_to_database()
    initialize_bounties_table_if_empty(bounties, db)
    differences = compare_with_existing_data(bounties, db)

    if differences:
        update_bounties_table(differences, db)
        insert_into_diff_table(differences, db)
        print(
            f"WARNING !!! Found {len(differences)} differences and saved them to MongoDB!"
        )

        for diff in differences:
            difference_str = (
                f"{diff['project']} has been updated!\n"
                f"Previous timestamp: {diff['existing_updatedDate'][:10]}  {diff['existing_updatedDate'][11:19]}\n"
                f"Current timestamp: {diff['new_updatedDate'][:10]}  {diff['new_updatedDate'][11:19]}\n"
                f"Link: https://immunefi.com/bounty/{diff['id']}/\n"
            )

            # Add the changed links
            if diff.get("link_diff"):
                difference_str += "Changed links:\n"
                for link in diff["link_diff"]:
                    difference_str += f"{link}\n"

            print(difference_str)
            send_message_to_telegram(difference_str)
    else:
        print("No differences found!")
        send_message_to_telegram("No differences found in the latest check!")

    time.sleep(600)