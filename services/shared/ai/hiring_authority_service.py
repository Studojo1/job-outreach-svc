from typing import List

def get_hiring_titles(role_name: str) -> List[str]:
    """Determine which titles actually hire the candidate's preferred role.
    
    Heuristics:
    Marketing roles -> Marketing Manager, Head of Marketing, Growth Manager
    SEO roles -> SEO Manager, Head of Growth
    Content roles -> Content Manager, Head of Content
    Video roles -> Creative Director, Video Production Manager
    Design roles -> Design Lead, Head of Design
    Product roles -> Product Manager, Head of Product
    Engineering roles -> Engineering Manager, Tech Lead
    
    Excludes: CEO, Founder, Intern, HR, Recruiter.
    Max titles: 3.
    """
    if not role_name:
        return ["Marketing Manager", "Growth Marketing Manager", "SEO Manager", "Content Marketing Manager"] # Generic fallback
        
    role_lower = role_name.lower()
    
    # Marketing Roles - Safety Rule: No VP/Director/Head
    if "marketing" in role_lower or "growth" in role_lower:
        return ["Digital Marketing Manager", "Growth Marketing Manager", "SEO Manager", "Content Marketing Manager"][:4]
    
    if "seo" in role_lower:
        return ["SEO Manager", "Digital Marketing Manager", "Growth Marketing Manager"][:4]
    elif "content" in role_lower or "copy" in role_lower:
        return ["Content Marketing Manager", "Digital Marketing Manager"][:4]
    elif "video" in role_lower or "production" in role_lower:
        return ["Creative Director", "Video Production Manager"][:4]
    elif "design" in role_lower or "creative" in role_lower:
        return ["Design Lead", "Head of Design"][:4]
    elif "product" in role_lower:
        return ["Product Manager", "Head of Product"][:4]
    elif "engineer" in role_lower or "develop" in role_lower or "tech" in role_lower:
        return ["Engineering Manager", "Tech Lead"][:4]
    else:
        # Broad fallback for unclassified roles (Marketing Manager base)
        return ["Digital Marketing Manager", "Growth Marketing Manager", "SEO Manager", "Content Marketing Manager"][:4]

