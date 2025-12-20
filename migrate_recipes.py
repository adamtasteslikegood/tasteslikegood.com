import os
import json
from app import migrate_recipe_data, RECIPES_DIR

def migrate_all():
    print(f"Starting migration in {RECIPES_DIR}...")
    count = 0
    migrated_files = []
    
    if not os.path.exists(RECIPES_DIR):
        print(f"Directory {RECIPES_DIR} not found.")
        return

    for filename in os.listdir(RECIPES_DIR):
        if not filename.endswith('.json'):
            continue
            
        filepath = os.path.join(RECIPES_DIR, filename)
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            
            data, changed = migrate_recipe_data(data, filename)

            if changed:
                with open(filepath, 'w') as f:
                    json.dump(data, f, indent=2)
                count += 1
                migrated_files.append(filename)
                print(f"Migrated: {filename}")
            else:
                print(f"Skipped (already up to date): {filename}")
                
        except Exception as e:
            print(f"Error migrating {filename}: {e}")
            
    print(f"\nMigration complete. {count} files updated.")
    if migrated_files:
        print("Updated files:")
        for f in migrated_files:
            print(f"- {f}")

if __name__ == "__main__":
    migrate_all()
