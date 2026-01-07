#!/usr/bin/env python3
"""
Publications Scraper for Academia.edu
Fetches publication data from Academia.edu and generates YAML data file.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml


class PublicationsScraper:
    """Scrapes publications from Academia.edu and generates YAML data."""
    
    ACADEMIA_URL = "https://syedmamun.academia.edu/research"
    RETRY_ATTEMPTS = 3
    TIMEOUT = 30
    
    def __init__(self, output_path: str):
        self.output_path = Path(output_path)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def fetch_page(self) -> Optional[str]:
        """Fetch the Academia.edu research page with retry logic."""
        for attempt in range(self.RETRY_ATTEMPTS):
            try:
                print(f"Fetching page (attempt {attempt + 1}/{self.RETRY_ATTEMPTS})...")
                response = self.session.get(self.ACADEMIA_URL, timeout=self.TIMEOUT)
                response.raise_for_status()
                print("✓ Page fetched successfully")
                return response.text
            except requests.RequestException as e:
                print(f"✗ Attempt {attempt + 1} failed: {e}")
                if attempt == self.RETRY_ATTEMPTS - 1:
                    print("Failed to fetch page after all retry attempts")
                    return None
        return None
    
    def extract_json_data(self, html: str) -> Optional[Dict]:
        """Extract JSON data from the HTML page."""
        try:
            # Find the React component JSON data
            pattern = r'<script type="application/json"[^>]*class="js-react-on-rails-component"[^>]*>(.*?)</script>'
            matches = re.findall(pattern, html, re.DOTALL)
            
            for match in matches:
                try:
                    data = json.loads(match)
                    if 'serializedStore' in data and 'works' in data['serializedStore']:
                        print(f"✓ Found {len(data['serializedStore']['works'])} publications")
                        return data['serializedStore']
                except json.JSONDecodeError:
                    continue
            
            print("✗ Could not find publication data in page")
            return None
        except Exception as e:
            print(f"✗ Error extracting JSON data: {e}")
            return None
    
    def parse_publications(self, data: Dict) -> List[Dict]:
        """Parse publication data into structured format."""
        works = data.get('works', [])
        sections_map = {s['id']: s['display_name'] for s in data.get('sections', [])}
        
        publications = []
        
        for work in works:
            pub = {
                'id': work.get('id'),
                'title': work.get('display_name', '').strip(),
                'authors': work.get('ordered_authors', []),
                'section': sections_map.get(work.get('section_id'), 'Other'),
                'url': work.get('url', ''),
                'external_url': work.get('external_url', ''),
                'thumbnail_url': work.get('thumbnail_url', ''),
                'display_order': work.get('display_order', 0),
            }
            
            # Use external URL if available, otherwise use the download URL
            if pub['external_url']:
                pub['url'] = pub['external_url']
            
            publications.append(pub)
        
        # Sort by display_order (ascending, since more negative = newer in Academia.edu)
        publications.sort(key=lambda x: x['display_order'], reverse=False)
        
        print(f"✓ Parsed {len(publications)} publications")
        return publications
    
    def group_by_section(self, publications: List[Dict]) -> Dict[str, List[Dict]]:
        """Group publications by section."""
        sections = {}
        for pub in publications:
            section = pub['section']
            if section not in sections:
                sections[section] = []
            sections[section].append(pub)
        
        print(f"✓ Grouped publications into {len(sections)} sections")
        return sections
    
    def generate_yaml(self, publications: List[Dict], sections: Dict[str, List[Dict]]) -> Dict:
        """Generate YAML data structure."""
        yaml_data = {
            'last_updated': datetime.now().strftime('%Y-%m-%d'),
            'total_publications': len(publications),
            'sections': {},
            'all_publications': []
        }
        
        # Add publications grouped by section
        for section_name, pubs in sections.items():
            yaml_data['sections'][section_name] = [
                {
                    'title': p['title'],
                    'authors': p['authors'],
                    'url': p['url'],
                    'thumbnail_url': p['thumbnail_url'],
                }
                for p in pubs[:50]  # Limit to 50 per section to avoid huge files
            ]
        
        # Add all publications (limited to most recent 100)
        yaml_data['all_publications'] = [
            {
                'title': p['title'],
                'authors': p['authors'],
                'section': p['section'],
                'url': p['url'],
                'thumbnail_url': p['thumbnail_url'],
            }
            for p in publications[:100]
        ]
        
        return yaml_data
    
    def save_yaml(self, data: Dict) -> bool:
        """Save YAML data to file."""
        try:
            # Ensure parent directory exists
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.output_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
            
            print(f"✓ YAML data saved to {self.output_path}")
            return True
        except Exception as e:
            print(f"✗ Error saving YAML file: {e}")
            return False
    
    def scrape(self) -> bool:
        """Main scraping method."""
        print("=" * 60)
        print("Publications Scraper for Academia.edu")
        print("=" * 60)
        
        # Fetch page
        html = self.fetch_page()
        if not html:
            return False
        
        # Extract JSON data
        data = self.extract_json_data(html)
        if not data:
            return False
        
        # Parse publications
        publications = self.parse_publications(data)
        if not publications:
            print("✗ No publications found")
            return False
        
        # Group by section
        sections = self.group_by_section(publications)
        
        # Generate YAML structure
        yaml_data = self.generate_yaml(publications, sections)
        
        # Save to file
        success = self.save_yaml(yaml_data)
        
        if success:
            print("=" * 60)
            print("✓ Scraping completed successfully!")
            print(f"  Total publications: {len(publications)}")
            print(f"  Sections: {', '.join(sections.keys())}")
            print("=" * 60)
        
        return success


def main():
    """Main entry point."""
    # Default output path
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    output_path = repo_root / '_data' / 'publications.yml'
    
    # Allow override via command line argument
    if len(sys.argv) > 1:
        output_path = Path(sys.argv[1])
    
    scraper = PublicationsScraper(str(output_path))
    success = scraper.scrape()
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
