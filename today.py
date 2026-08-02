import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib
import re

# Configuration
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0,
               'loc_query': 0}
EXCLUDED_REPOS = {'is-a-dev/register', 'is-a-good-dev/register'}


def daily_readme(birthday):
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables},
                            headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def fetch_streak(username):
    """
    Fetches GitHub streak data.
    The API returns an SVG; we parse the 'Current Streak' value from the XML.
    """
    url = f"https://github-readme-streak-stats-vijaypur.vercel.app/?user={username}"
    for delay in [1, 2, 4]:
        try:
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                svg_text = response.text
                # The "Current Streak" big number is inside a <text> element
                # that has the 'currstreak' animation style applied to it.
                # We use a non-greedy match to find the first occurrence of the number
                # inside a text tag specifically associated with that animation.
                match = re.search(r"style=['\"]animation:\s*currstreak[^>]*>[\s\n]*([0-9,]+)[\s\n]*</text>", svg_text,
                                  re.DOTALL)

                if match:
                    return match.group(1).strip()
        except Exception:
            time.sleep(delay)
    return "N/A"


def graph_commits():
    query_count('graph_commits')
    query = '''
    query($login: String!) {
        user(login: $login) {
            contributionsCollection {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    data = request.json()['data']['user']['contributionsCollection']
    return int(data['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    global OWNER_ID
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    while True:
        query_count('recursive_loc')
        variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
        request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables},
                                headers=HEADERS)
        if request.status_code != 200:
            force_close_file(data, cache_comment)
            if request.status_code == 403:
                raise Exception(
                    'Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
            raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)

        if request.json()['data']['repository']['defaultBranchRef'] is None:
            return 0

        history = request.json()['data']['repository']['defaultBranchRef']['target']['history']
        for node in history['edges']:
            if node['node']['author']['user'] and node['node']['author']['user']['id'] == OWNER_ID:
                my_commits += 1
                addition_total += node['node']['additions']
                deletion_total += node['node']['deletions']

        if not history['edges'] or not history['pageInfo']['hasNextPage']:
            return addition_total, deletion_total, my_commits
        cursor = history['pageInfo']['endCursor']


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    if edges is None:
        edges = []
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    while True:
        query_count('loc_query')
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        request = simple_request(loc_query.__name__, query, variables)
        repo_data = request.json()['data']['user']['repositories']
        edges += [e for e in repo_data['edges']
                  if e and e.get('node') and e['node']['nameWithOwner'] not in EXCLUDED_REPOS]
        if not repo_data['pageInfo']['hasNextPage']:
            return cache_builder(edges, comment_size, force_cache)
        cursor = repo_data['pageInfo']['endCursor']


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    edges = [e for e in edges if e is not None and e.get('node') is not None
             and e['node']['nameWithOwner'] not in EXCLUDED_REPOS]
    print(edges)
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'

    if not os.path.exists('cache'):
        os.makedirs('cache')

    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]

    for index in range(len(edges)):
        line_parts = data[index].split()
        repo_hash = line_parts[0]
        commit_count = line_parts[1]

        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                hist = edges[index]['node']['defaultBranchRef']['target']['history']
                if int(commit_count) != hist['totalCount']:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = f"{repo_hash} {hist['totalCount']} {loc[2]} {loc[0]} {loc[1]}\n"
            except (TypeError, AttributeError):
                data[index] = repo_hash + ' 0 0 0 0\n'

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, True]


def flush_cache(edges, filename, comment_size):
    with open(filename, 'r') as f:
        data = []
        lines = f.readlines()
        if len(lines) >= comment_size:
            data = lines[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            if node.get('node') and node['node'].get('nameWithOwner'):
                f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)


def stars_counter(data):
    total_stars = 0
    for node in data: total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def committers_rank_getter(username, country='kurdistan'):
    url = f"https://user-badge.committers.top/{country}_private/{username}.svg"
    response = requests.get(url, timeout=15)
    if response.status_code != 200:
        return 'Unranked'
    return extract_rank_from_committers_svg(response.text)


def extract_rank_from_committers_svg(svg_text):
    if re.search(r"\bunranked\b", svg_text, flags=re.IGNORECASE):
        return 'Unranked'
    match = re.search(r"#\s*([0-9][0-9,]*)", svg_text)
    if match:
        return int(match.group(1).replace(',', ''))
    return 'Unranked'


def svg_overwrite(filename, display_name, subtitle_data, age_data, commit_data, streak_data, rank_data, repo_data, contrib_data,
                  follower_data, loc_data, initials):
    tree = etree.parse(filename)
    root = tree.getroot()

    # Standard formats
    justify_format(root, 'display_name', display_name, 0)
    justify_format(root, 'subtitle_data', subtitle_data, 0)
    justify_format(root, 'initials', initials, 0)
    justify_format(root, 'age_data', age_data, 0)
    justify_format(root, 'repo_data', repo_data, 0)
    justify_format(root, 'contrib_data', contrib_data, 0)
    justify_format(root, 'follower_data', follower_data, 0)
    justify_format(root, 'loc_data', loc_data[2], 0)
    justify_format(root, 'streak_data', streak_data, 0)

    # Custom Prefixes
    # Add 10 to accommodate for the 2 commits done in the excluded repos which are too large to parse
    justify_format(root, 'commit_data', f"Commits: {commit_data + 2}", 0)
    justify_format(root, 'rank_data', f"#{rank_data}", 0)
    justify_format(root, 'loc_add', f"++ {loc_data[0]}", 0)
    justify_format(root, 'loc_del', f"-- {loc_data[1]}", 0)

    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, total_width):
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)

    if total_width > 0:
        dots_needed = total_width - len(new_text)
        dot_string = ' ' + ('.' * (dots_needed - 2)) + ' ' if dots_needed > 2 else ''
        find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def user_getter(username):
    query_count('user_getter')
    query = 'query($login: String!){ user(login: $login) { id createdAt name login company bio avatarUrl } }'
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    user = request.json()['data']['user']
    return user['id'], user['createdAt'], user['name'], user['login'], user['company'], user['bio'], user['avatarUrl']


def follower_getter(username):
    query_count('follower_getter')
    query = 'query($login: String!){ user(login: $login) { followers { totalCount } } }'
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference):
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    if difference > 1:
        print('{:>12}'.format('%.4f' % difference + ' s '))
    else:
        print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))


if __name__ == '__main__':
    print('Starting stats update...')
    OWNER_ID, acc_date, display_name, login_name, company, bio, avatar_url = user_getter(USER_NAME)
    formatter('account data', 0)

    account_created_at = datetime.datetime.fromisoformat(acc_date.replace('Z', '+00:00')).replace(tzinfo=None)
    age_data, age_time = perf_counter(daily_readme, account_created_at)
    formatter('age calculation', age_time)

    display_name = display_name or 'Abhin Tiwari'
    subtitle_data = company or bio or 'GitHub Profile README'
    initials = ''.join(part[0] for part in display_name.split() if part)[:2].upper() or login_name[:2].upper()

    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time)

    commit_data, _ = perf_counter(graph_commits)
    streak_data, _ = perf_counter(fetch_streak, USER_NAME)
    rank_data, _ = perf_counter(committers_rank_getter, USER_NAME)
    repo_data, _ = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, _ = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, _ = perf_counter(follower_getter, USER_NAME)

    # Prepare comma formatted strings for SVG overwrite
    loc_formatted = [
        '{:,}'.format(total_loc[0]),
        '{:,}'.format(total_loc[1]),
        '{:,}'.format(total_loc[2])
    ]

    svg_overwrite('dark_mode.svg', display_name, subtitle_data, age_data, commit_data, streak_data, rank_data, repo_data,
                  contrib_data, follower_data, loc_formatted, initials)
    svg_overwrite('light_mode.svg', display_name, subtitle_data, age_data, commit_data, streak_data, rank_data, repo_data,
                  contrib_data, follower_data, loc_formatted, initials)

    print('Total GitHub GraphQL API calls:', sum(QUERY_COUNT.values()))
