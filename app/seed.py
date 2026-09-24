# ruff: noqa
import os
from google.cloud import firestore

PROJECT_ID = "qwiklabs-gcp-01-102b006ec7cf"

def seed_firestore():
    print(f"Connecting to Firestore with project_id={PROJECT_ID}...")
    db = firestore.Client(project=PROJECT_ID)

    destinations = [
        {
            "id": "kyoto",
            "name": "Kyoto",
            "country": "Japan",
            "category": "Cultural & Historic",
            "description": "Historic city known for classical Buddhist temples, traditional gardens, imperial palaces, and wooden houses.",
            "best_season": "Spring & Autumn",
            "avg_budget_usd": 180,
            "tags": ["temples", "culture", "nature", "food"],
        },
        {
            "id": "paris",
            "name": "Paris",
            "country": "France",
            "category": "Urban & Art",
            "description": "France's capital, a major European city and global center for art, fashion, gastronomy and culture.",
            "best_season": "Spring & Summer",
            "avg_budget_usd": 220,
            "tags": ["art", "museums", "romance", "gastronomy"],
        },
        {
            "id": "bali",
            "name": "Bali",
            "country": "Indonesia",
            "category": "Tropical & Wellness",
            "description": "Indonesian island famous for its iconic rice paddies, beaches, coral reefs, and spiritual retreats.",
            "best_season": "Dry Season (April to October)",
            "avg_budget_usd": 90,
            "tags": ["beaches", "wellness", "surfing", "temples"],
        },
        {
            "id": "serengeti",
            "name": "Serengeti National Park",
            "country": "Tanzania",
            "category": "Wildlife & Safari",
            "description": "Vast African ecosystem world-famous for its annual migration of over 1.5 million wildebeest and zebra.",
            "best_season": "June to October",
            "avg_budget_usd": 350,
            "tags": ["safari", "wildlife", "nature", "adventure"],
        },
    ]

    for dest in destinations:
        doc_ref = db.collection("destinations").document(dest["id"])
        doc_ref.set(dest)
        print(f"Seeded destination: {dest['name']} ({dest['id']})")

    print("Firestore seeding complete!")

if __name__ == "__main__":
    seed_firestore()
