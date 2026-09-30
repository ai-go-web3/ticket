"""灌演示数据：影片 / 影院 / 影厅 / 排期 / 用户 / 示例订单。

幂等：可重复执行，已存在的 up_ 主键跳过。金额统一「分」。
运行：python manage.py shell < scripts/seed_demo.py
"""
import os
import sys
from datetime import timedelta

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.utils import timezone
from apps.catalog.models import City, Movie, Cinema, CinemaHall, Schedule
from apps.auths.models import AppUser

now = timezone.now()
today = now.date()


def _dt(day_offset, hhmm):
    h, m = hhmm.split(':')
    d = (now + timedelta(days=day_offset)).replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    return d


# ===== 影片 =====
# (up_movie_id, name, status, type, duration, rating, region, language,
#  actors, release_offset, director, want_count, presale, description)
MOVIES = [
    # ---- 热映 status=1 ----
    ('mv_001', '流浪地球3', 1, '科幻/冒险', 165, '9.2', '中国大陆', '国语',
     '吴京,刘德华,李雪健', -30, '郭帆', 1280000, 1,
     '太阳即将毁灭，人类在地球表面建造出巨大的推进器，寻找新的家园。'),
    ('mv_002', '老江湖', 1, '犯罪', 118, None, '中国大陆', '国语',
     '于谦,梁家辉,王影璐', -10, '何念', 320000, 0,
     '一场江湖恩怨，牵出尘封多年的惊天秘密。'),
    ('mv_003', '敦煌英雄', 1, '古装/战争', 132, '8.6', '中国大陆', '国语',
     '章宇,窦骁,吕凉', -20, '曹盾', 540000, 1,
     '大唐边塞，一群无名英雄书写家国传奇。'),
    ('mv_004', '空枪', 1, '剧情/犯罪', 125, '9.5', '中国大陆', '国语',
     '朱一龙,檀健次,梁家辉', -5, '高群书', 760000, 1,
     '扣不动的扳机，藏不住的真相。'),
    ('mv_005', '神探之痕迹', 1, '悬疑/犯罪', 110, '8.7', '中国大陆', '国语',
     '张译,马丽,陈明昊,王俊凯', -15, '陈思诚', 980000, 0,
     '重案解封，硬核必看，神探归来追寻蛛丝马迹。'),
    ('mv_006', '复仇者联盟4：终局之战', 1, '动作/科幻', 181, '9.1', '美国', '英语',
     '小罗伯特·唐尼,克里斯·埃文斯,斯嘉丽·约翰逊', -60, '安东尼·罗素,乔·罗素', 1500000, 0,
     '超级英雄集结，逆转无限，守护宇宙最后一线希望。'),
    ('mv_009', '什么意思夫妇', 1, '剧情/家庭', 105, '8.7', '中国大陆', '国语',
     '小沈阳,沈春阳,贾冰,于洋', -3, '沈春阳', 210000, 0,
     '十一欢乐喜剧，夫妻搭档笑闹天翻地覆。'),
    ('mv_010', '欢迎来龙餐馆', 1, '喜剧/剧情', 132, '9.9', '中国大陆', '国语',
     '沈腾,蒋奇明,奥马尔·谢里夫', -25, '文牧野', 1350000, 1,
     '全家有笑有泪有劲爆，一间餐馆道尽人间烟火。'),

    # ---- 待映 status=2（覆盖未来 14 天，供日历展示）----
    ('mv_007', '封神第二部', 2, '奇幻/战争', None, None, '中国大陆', '国语',
     None, 2, '乌尔善', 123000, 1, '封神宇宙续作，大战一触即发。'),
    ('mv_008', '深海秘境', 2, '动画/冒险', None, None, '中国大陆', '国语',
     None, 1, '田晓鹏', 51000, 1, '潜入深海，探索未知秘境。'),
    ('mv_101', '人生二分之一', 2, '剧情/悬疑', None, None, '中国大陆', '国语',
     None, 0, '卢星宇', 830, 0, '一场关于选择与命运的灵魂追问。'),
    ('mv_102', '古宅绣花鞋', 2, '惊悚/悬疑', None, None, '中国大陆', '国语',
     '赵樱子,管栎,九孔,骆达华', 0, '卢星宇', 5186, 0, '深宅旧事，一双绣花鞋牵出百年凶案。'),
    ('mv_103', '小猪佩奇·完美假期', 2, '动画/家庭', None, None, '英国', '国语',
     '陈奕雯,张安琪,范楚绒,符冲', 0, '刘源', 77000, 1, '佩奇一家开启欢乐的完美假期。'),
    ('mv_104', '暗夜行者', 2, '悬疑/犯罪', None, None, '中国大陆', '国语',
     '彭昱畅,张钧甯,王砚辉', 0, '陈正道', 42000, 1, '夜色之下，真相与谎言一线之隔。'),
    ('mv_105', '长安三万里·续', 2, '动画/历史', None, None, '中国大陆', '国语',
     None, 0, '谢君伟', 96000, 0, '诗酒长安，再续大唐风华。'),
    ('mv_106', '火种', 2, '剧情/战争', None, None, '中国大陆', '国语',
     '张颂文,王传君', 0, '陈凯歌', 12000, 0, '星火燎原，一段不能忘却的历史。'),
    ('mv_107', '熊出没·逆转时空', 2, '动画/喜剧', None, None, '中国大陆', '国语',
     None, 1, '林汇达', 33000, 1, '光头强与熊大熊二穿越时空的全新冒险。'),
    ('mv_108', '九龙城寨之围城', 2, '动作/犯罪', None, None, '中国香港', '粤语',
     '古天乐,洪金宝,任贤齐', 2, '郑保瑞', 26000, 0, '九龙城寨，一场宿命般的围城之战。'),
    ('mv_109', '头脑特工队3', 2, '动画/奇幻', None, None, '美国', '英语',
     None, 2, '彼特·道格特', 88000, 1, '情绪小人们迎来全新成员与挑战。'),
    ('mv_110', '我不是药神2', 2, '剧情', None, None, '中国大陆', '国语',
     '徐峥,王传君,谭卓', 3, '文牧野', 63000, 0, '小人物的大挣扎，生命的重量再度被叩问。'),
    ('mv_111', '明日战记2', 2, '科幻/动作', None, None, '中国香港', '粤语',
     '古天乐,刘青云,刘嘉玲', 4, '吴炫辉', 45000, 1, '机甲再临，为人类明日而战。'),
    ('mv_112', '大鱼海棠2', 2, '动画/奇幻', None, None, '中国大陆', '国语',
     None, 4, '梁旋,张春', 74000, 0, '海棠花开，续写奇幻世界的宿命轮回。'),
    ('mv_113', '湄公河行动2', 2, '动作/犯罪', None, None, '中国大陆', '国语',
     '张涵予,彭于晏', 4, '林超贤', 38000, 0, '雷霆出击，跨境剿匪再度上演。'),
    ('mv_114', '蜘蛛侠：超越宇宙', 2, '动画/科幻', None, None, '美国', '英语',
     None, 4, '华金·多斯桑托斯', 152000, 1, '迈尔斯跨越多元宇宙，迎战命运。'),
    ('mv_115', '白蛇：浮生', 2, '动画/奇幻', None, None, '中国大陆', '国语',
     None, 5, '陈健', 29000, 0, '千年等一回，白蛇传说全新演绎。'),
    ('mv_116', '冲出地球', 2, '动画/科幻', None, None, '中国大陆', '国语',
     None, 5, '沈乐平', 15000, 0, '少年与机甲，冲向浩瀚星辰。'),
    ('mv_117', '志愿军3', 2, '剧情/战争', None, None, '中国大陆', '国语',
     '吴京,张译,段奕宏', 6, '陈凯歌', 56000, 1, '铁血军魂，跨越时空的家国记忆。'),
    ('mv_118', '消失的她2', 2, '悬疑/犯罪', None, None, '中国大陆', '国语',
     '朱一龙,倪妮,文咏珊', 6, '崔睿', 41000, 0, '真相层层剥开，人性深不见底。'),
    ('mv_119', '宇宙探索编辑部2', 2, '科幻/喜剧', None, None, '中国大陆', '国语',
     '杨皓宇,艾丽娅', 6, '孔大山', 8200, 0, '一群怪人踏上寻找外星人的荒诞之旅。'),
    ('mv_120', '无名之辈2', 2, '剧情/喜剧', None, None, '中国大陆', '国语',
     '章宇,任素汐,潘斌龙', 7, '饶晓志', 34000, 1, '小人物也有大梦想，笑着笑着就哭了。'),
    ('mv_121', '熊出没·奇幻空间', 2, '动画/喜剧', None, None, '中国大陆', '国语',
     None, 7, '林汇达', 22000, 0, '熊强组合闯入奇幻空间，欢乐升级。'),
    ('mv_122', '三体：黑暗森林', 2, '科幻', None, None, '中国大陆', '国语',
     '张鲁一,于和伟,陈瑾', 8, '张番番', 210000, 1, '宇宙就是一座黑暗森林，每个文明都是带枪的猎人。'),
    ('mv_123', '哪吒之魔童闹海2', 2, '动画/奇幻', None, None, '中国大陆', '国语',
     None, 8, '饺子', 320000, 1, '我命由我不由天，哪吒归来再战乾坤。'),
    ('mv_124', '平原上的火焰', 2, '剧情/悬疑', None, None, '中国大陆', '国语',
     '周冬雨,刘昊然', 8, '张骥', 18000, 0, '一场大火，烧尽了青春与秘密。'),
    ('mv_125', '寻龙诀2', 2, '奇幻/冒险', None, None, '中国大陆', '国语',
     '陈坤,黄渤,舒淇', 9, '乌尔善', 47000, 0, '摸金校尉再入古墓，探寻千年秘境。'),
    ('mv_126', '误杀4', 2, '悬疑/犯罪', None, None, '中国大陆', '国语',
     '肖央,谭卓', 12, '柯汶利', 39000, 0, '为爱犯险，父爱的极限再被推向深渊。'),
    ('mv_127', '反贪风暴6', 2, '动作/犯罪', None, None, '中国香港', '粤语',
     '古天乐,张智霖', 12, '林德禄', 11000, 0, '反贪利剑再出鞘，直击金融黑幕。'),
]

movie_ids = {}
for (up_id, name, status, typ, dur, rating, region, lang, actors,
     r_offset, director, want, presale, desc) in MOVIES:
    rdate = today + timedelta(days=r_offset)
    obj, created = Movie.objects.update_or_create(
        up_movie_id=up_id,
        defaults={
            'name': name, 'status': status, 'type': typ, 'duration': dur,
            'rating': rating, 'region': region, 'language': lang, 'actors': actors,
            'release_date': rdate, 'director': director, 'want_count': want,
            'presale': presale, 'description': desc,
        },
    )
    movie_ids[up_id] = obj
    print(f"[movie] {'+' if created else '='} {obj.id} {name} status={status} want={want}")

# 清掉早期种子遗留、已不在列表中的影片（避免脏数据）
stale = Movie.objects.exclude(up_movie_id__in=[m[0] for m in MOVIES]).filter(status=2)
stale.update(deleted=1)

# ===== 影院（成都 7 家）=====
CINEMAS = [
    # (up_cinema_id, name, region, brand, address, lng, lat, phone, supports)
    ('cn_001', 'CGV影城（春熙路旗舰店）', '锦江区', 'CGV', '锦江区中纱街8号', 104.081000, 30.657000, '028-88886666', 'IMAX,杜比全景声,4DX,支持退票'),
    ('cn_002', '万达影城（金牛万达广场）', '金牛区', '万达', '金牛区一环路北三段1号', 104.056000, 30.688000, '028-66667777', '巨幕,可改签'),
    ('cn_003', '太平洋影城（王府井店）', '锦江区', '太平洋', '锦江区总府路12号', 104.076000, 30.662000, '028-99990000', '4DX,支持退票'),
    ('cn_004', 'SFC上影国际影城（成都IFS店）', '锦江区', 'SFC', '锦江区红星路三段1号IFS国际金融中心6楼', 104.080500, 30.657500, '028-86668888', 'IMAX,杜比全景声'),
    ('cn_005', '太平洋影城（泛悦国际店）', '武侯区', '太平洋', '武侯区人民南路四段3号泛悦国际4楼', 104.043000, 30.620000, '028-85556666', '巨幕,支持退票'),
    ('cn_006', '万达影城（青羊万达广场）', '青羊区', '万达', '青羊区日月大道一段978号', 104.050000, 30.670000, '028-87773333', 'IMAX,可改签'),
    ('cn_007', '保利国际影城（成都奥克斯店）', '武侯区', '保利', '武侯区天府大道北段1700号奥克斯广场', 104.062000, 30.600000, '028-85221111', '巨幕,4DX'),
]

cinema_ids = {}
for up_id, name, region, brand, address, lng, lat, phone, supports in CINEMAS:
    obj, created = Cinema.objects.update_or_create(
        up_cinema_id=up_id,
        defaults={
            'name': name, 'city_code': '8', 'region': region, 'brand': brand,
            'address': address, 'lng': lng, 'lat': lat, 'phone': phone,
            'supports': supports, 'business_status': 1,
        },
    )
    cinema_ids[up_id] = obj
    print(f"[cinema] {'+' if created else '='} {obj.id} {name}")

# ===== 影厅 =====
HALLS = [
    ('cn_001', 'hall_10', '10号厅', 'IMAX'),
    ('cn_001', 'hall_05', '5号厅', '3D'),
    ('cn_002', 'hall_01', '1号厅', '巨幕'),
    ('cn_002', 'hall_03', '3号厅', '普通'),
    ('cn_003', 'hall_07', '7号厅', '4DX'),
    ('cn_004', 'hall_02', '2号厅', 'IMAX'),
    ('cn_005', 'hall_04', '4号厅', '巨幕'),
    ('cn_006', 'hall_06', '6号厅', 'IMAX'),
    ('cn_007', 'hall_08', '8号厅', '普通'),
]
for cin_up, hall_up, hall_name, hall_type in HALLS:
    cid = cinema_ids[cin_up].id
    CinemaHall.objects.update_or_create(
        cinema_id=cid, up_hall_id=hall_up,
        defaults={'hall_name': hall_name, 'hall_type': hall_type},
    )
print('[hall] 影厅已灌入')

# ===== 排期（多影院 × 热门影片，覆盖今天/明天）=====
SCHEDULES = [
    # (up_schedule_id, cinema_up, movie_up, hall_name, day_offset, hhmm, show_type, price_yuan, remain, refundable)
    ('sh_001', 'cn_001', 'mv_001', '10号厅', 0, '15:30', 'IMAX 2D', 45, 68, 1),
    ('sh_002', 'cn_001', 'mv_001', '10号厅', 0, '21:10', 'IMAX 2D', 48, 3, 1),
    ('sh_003', 'cn_001', 'mv_004', '5号厅', 0, '18:20', '3D', 39.9, 12, 1),
    ('sh_004', 'cn_001', 'mv_002', '5号厅', 1, '19:40', '2D', 35, 80, 1),
    ('sh_005', 'cn_002', 'mv_001', '1号厅', 0, '14:00', '巨幕 2D', 42, 120, 1),
    ('sh_006', 'cn_002', 'mv_003', '3号厅', 0, '20:30', '2D', 38, 40, 1),
    ('sh_007', 'cn_002', 'mv_005', '3号厅', 1, '13:10', '2D', 36, 60, 1),
    ('sh_008', 'cn_003', 'mv_006', '7号厅', 0, '16:45', '4DX', 55, 20, 0),
    ('sh_009', 'cn_003', 'mv_004', '7号厅', 1, '18:00', '4DX', 52, 15, 1),
    ('sh_010', 'cn_003', 'mv_002', '7号厅', 0, '11:20', '2D', 33, 100, 1),
    ('sh_011', 'cn_004', 'mv_005', '2号厅', 0, '19:30', 'IMAX 2D', 46, 88, 1),
    ('sh_012', 'cn_004', 'mv_010', '2号厅', 0, '13:00', 'IMAX 2D', 52, 110, 1),
    ('sh_013', 'cn_005', 'mv_010', '4号厅', 0, '16:10', '巨幕 2D', 43, 90, 1),
    ('sh_014', 'cn_005', 'mv_009', '4号厅', 1, '20:00', '2D', 32, 70, 1),
    ('sh_015', 'cn_006', 'mv_006', '6号厅', 0, '21:00', 'IMAX 3D', 58, 50, 0),
    ('sh_016', 'cn_006', 'mv_003', '6号厅', 1, '15:00', 'IMAX 2D', 44, 100, 1),
    ('sh_017', 'cn_007', 'mv_004', '8号厅', 0, '17:30', '2D', 36, 60, 1),
    ('sh_018', 'cn_007', 'mv_001', '8号厅', 1, '20:20', '2D', 40, 80, 1),
]

for up_sid, cin_up, mv_up, hall, doff, hhmm, show_type, price, remain, refundable in SCHEDULES:
    cid = cinema_ids[cin_up].id
    mid = movie_ids[mv_up].id
    start = _dt(doff, hhmm)
    duration = movie_ids[mv_up].duration or 120
    end = start + timedelta(minutes=duration)
    Schedule.objects.update_or_create(
        up_schedule_id=up_sid,
        defaults={
            'cinema_id': cid, 'movie_id': mid, 'hall_name': hall,
            'start_at': start, 'end_at': end, 'show_type': show_type,
            'language': '国语', 'min_price': int(price * 100),
            'remain_seats': remain, 'refundable': refundable, 'endorseable': 1,
            'sell_status': 1, 'snapshot_at': now,
        },
    )
print('[schedule] 排期已灌入')

# ===== 测试用户 =====
demo_user, _ = AppUser.objects.update_or_create(
    openid='demo_openid_0001',
    defaults={
        'nickname': '电影达人', 'phone': '13888880000', 'phone_mask': '138****8000',
        'city_code': '8', 'status': 1,
    },
)
print(f'[user] demo user id={demo_user.id} openid={demo_user.openid}')

print('\n=== 灌数据完成 ===')
print(f"影片 {Movie.objects.filter(deleted=0).count()} / 影院 {Cinema.objects.count()} / 排期 {Schedule.objects.count()} / 用户 {AppUser.objects.count()}")
