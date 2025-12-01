let DisconSchedule = {
    streets: undefined,
    selectelem: false,
    finded_count: undefined,
    preset: undefined,
    fact: undefined,
    showCurOutage: undefined,
    showCurSchedule: undefined,
    showTableSchedule: undefined,
    showTablePlan: undefined,
    showTableFact: undefined,
    updateTimestamp: undefined,
    currentWeekDayIndex: undefined,
    timeoutUpdateData: Date.now(),
    timeoutNotUse: Date.now(),
    timeoutUpdateCheck: 0,
    limitUpdateTime: 15, //minutes
    limitNotUseTime: 20, //seconds
    limitUpdateCheck: 5, //minutes
    timerCheck: null,
    messages: new Map([
        ['no-blackout', "Якщо в даний момент у вас відсутнє світло, імовірно виникла аварійна ситуація, або діють стабілізаційні або екстрені відключення. Просимо перевірити інформацію через 15 хвилин, саме стільки часу потрібно для оновлення даних на сайті."],
        ['under-discon', 'Для отримання інформації про графік відключень за Вашою адресою звертайтеся до ОСББ/ЖЕК/Керуючої компанії.'],
        ['extra-message', 'У разі відсутності світла у зоні, що гарантує його наявність (на графіку – білий колір), оформіть заявку нижче.'],
        ['discon-not-actual', ''],//Наразі графік стабілізаційних відключень не застосовується.
        ['planned-discon', ''],//Нижче можна ознайомитись з графіком стабілізаційних відключень для Вашого будинку
        ['leave-message', ''],//Графік стабілізаційних відключень не застосовується для Вас.</br>Якщо ж відключення відбуваються за відсутністю будинку у графіку - просимо повідомити нас про це натиснувши кнопку нижче. Ми додатково перевіримо інформацію та додамо адресу у разі потреби.
        ['discon-today-empty','Очікуйте на оновлення графіків'],
        ['discon-tomorrow-empty','Інформація від НЕК «Укренерго» про обмеження на наступний день ще не надходила'],
        ['discon-multigroup','Наразі інформація по графіку відключень за вашою адресою відсутня'],
    ]),
    form: $('#discon_form'),
    group: undefined,
    multiGroup: undefined,
    onload: document.addEventListener('DOMContentLoaded', function () {
        DisconSchedule.init();
    }),
    init: function () {
        this.ajax.url = document.querySelector('meta[name="ajaxUrl"]').content;
        $('#discon_form #house_num').prop('disabled', true);
        $('#discon_form #street').prop('disabled', true);
        if (DisconSchedule.fact?.update) DisconSchedule.form.append($('<input/>',{name:'updateFact',type:'hidden',value:DisconSchedule.fact.update}));
        if ((DisconSchedule.fact.data.length==0 && DisconSchedule.preset.data.length==0) || DisconSchedule.preset.time_zone.length==0) {
            DisconSchedule.showCurSchedule = false;
            DisconSchedule.showTableFact = false;
            DisconSchedule.showTableSchedule = false;
        }
        DisconSchedule.tableRender(0);
        $('#showCurOutage').removeClass('active');
        this.bind();
        this.checkTimeout();
    },
    bind: function () {
        DisconSchedule.autocomplete($('#discon_form #city')[0], Object.keys(DisconSchedule.streets), false);
        $('#discon_form .autocomplete img').on('click', DisconSchedule.listopen);
        $('#discon_form .form__input').on('click', DisconSchedule.listopen_input);

        $('.legend-item>#schedulled-text').text(DisconSchedule.preset['time_type']['no']);
        $('.legend-item>#non-schedulled-text').text(DisconSchedule.preset['time_type']['yes']);
        $('.legend-item>#maybe-schedulled-text').text(DisconSchedule.preset['time_type']['maybe']);
        $('.legend-item>#first-schedulled-text').text(DisconSchedule.preset['time_type']['mfirst']);
        $('.legend-item>#second-schedulled-text').text(DisconSchedule.preset['time_type']['msecond']);

        $('.discon-schedule-table')[0].onfocus = function (event) {
            $('.discon-schedule-table')[0].blur();
            event.stopPropagation();
        }
        $(document).on('click','.discon-fact .dates .date', function(e){
            if ($(this).hasClass('active')) return;
            const section = $(this).parent().parent();
            let rel = $(this).attr('rel');
            if (section.find('[rel='+rel+']').length) {
                section.find('.discon-fact-tables > *').removeClass('active');
                section.find('.discon-fact-tables > [rel='+rel+']').addClass('active');
                section.find('.date').removeClass('active');
                $(this).addClass('active');
            }
            DisconSchedule.form.data('activeTab',rel);
        });
        window.addEventListener("resize", function(event) {
            if ($(".discon-schedule-table").hasClass('active')) DisconSchedule.tableRender(DisconSchedule.group, DisconSchedule.multiGroup);
        });
    },
    checkTimeout: function() {
        if (Date.now() - DisconSchedule.timeoutUpdateData > DisconSchedule.limitUpdateTime * 60000) {
            if (Date.now() - DisconSchedule.timeoutNotUse > DisconSchedule.limitNotUseTime * 1000) {
                DisconSchedule.timeoutUpdateData = Date.now();
                DisconSchedule.ajax.getStreetsInvisibly();
            }
        }
        setTimeout(DisconSchedule.checkTimeout, DisconSchedule.limitNotUseTime * 1000);
    },
    checkUpdateTimeout: function(start = false) {
        var isRun = (DisconSchedule.timeoutUpdateCheck)?true:false;
        if (start) {
            DisconSchedule.timeoutUpdateCheck = Date.now();
            if (isRun) return;
        }
        else if (start === null) {
            DisconSchedule.timeoutUpdateCheck = 0;
            if (DisconSchedule.timerCheck) {
                clearTimeout(DisconSchedule.timerCheck);
                return;
            }
        }
        else {
            if (DisconSchedule.timeoutUpdateCheck && Date.now() - DisconSchedule.timeoutUpdateCheck > DisconSchedule.limitUpdateCheck * 60000) {
                DisconSchedule.timeoutUpdateCheck = Date.now();
                DisconSchedule.ajax.checkDisconUpdate();
            }
        }
        if (DisconSchedule.timeoutUpdateCheck) {
            DisconSchedule.timerCheck = setTimeout(DisconSchedule.checkUpdateTimeout, 10000);
        }
    },
    autocomplete: function (inp, data, key_preset) {
        /*the autocomplete function takes two arguments,
        the text field element and an array of possible autocompleted values:*/
        var currentFocus;
        if (inp.id == 'house_num' && key_preset[0]["sub_type_reason"].length > 1 && data.length == 1 && data[0] == '-' && DisconSchedule.showTableSchedule) {
            $('.discon-schedule-alert').addClass('active');
        }
        else {
            $('.discon-schedule-alert').removeClass('active');
        }
        if (inp.id == 'street' && data[0] == "" && data.length == 1) {
            closeAllLists();
            $('#discon_form #house_num')[0].value = " ";
            $('#discon_form #house_num').prop('disabled', true);
            $('#discon_form #house_num').removeClass('active');
            $('#discon_form #street')[0].value = " ";
            $('#discon_form #street').prop('disabled', true);
            $('#discon_form #street').removeClass('active');
            $('#discon_form .error-active').removeClass('error-active');
            $('#showCurOutage').removeClass('active');
            $('.discon-schedule-table').addClass('active');
            if (DisconSchedule.showCurSchedule && DisconSchedule.showTableSchedule) $('#legendarium-table').addClass('active');
            DisconSchedule.ajax.formSubmit('getHomeNum');
            return;
        }
        if (data[0] == '-') {
            closeAllLists();
            $('#discon_form #house_num')[0].value = " ";
            $('#discon_form #house_num').prop('disabled', true);
            $('#discon_form #house_num').removeClass('active');
            $('#discon_form .error-active').removeClass('error-active');
            $('#showCurOutage').removeClass('active');
            $('.discon-schedule-table').addClass('active');
            if (DisconSchedule.showTableSchedule) $('.discon-schedule-title').addClass('active');
            if (DisconSchedule.showCurSchedule && DisconSchedule.showTableSchedule) $('#legendarium-table').addClass('active');
            DisconSchedule.alertMessageBlock(key_preset,0);
            return;
        }
        const inputFunc = function (e) {
            DisconSchedule.timeoutNotUse = Date.now();
            DisconSchedule.selectelem = false;
            var a, b, val = this.value; //Видалили i магію
            closeAllLists();
            if ((inp.name == "street" || inp.name == "city") && inp.value == '') DisconSchedule.checkUpdateTimeout(null);
            if (!val) return false;
            if (key_preset === false && val.length < 3) return false;
            if (inp.name == "street" && data.length == 1 && data[0] == '.') {
                DisconSchedule.disableWrapper('street');
                inp.value=' ';
                DisconSchedule.disableWrapper('house_num');
                $('#discon_form #house_num')[0].value=' ';
                DisconSchedule.alertMessageBlock(key_preset,0);
                DisconSchedule.ajax.getHomeNumInvisibly();
                return;
            }
            if (inp.name == "house_num" && data.length == 1 && data[0] == '.') {
                if (key_preset[0]["sub_type_reason"].length > 1) {
                    $('#discon_form #group-name').css("display", "none");
                    DisconSchedule.tableRender(0, true);
                    if (DisconSchedule.showTableSchedule) $('.discon-schedule-alert').addClass('active');
                }
                else if(key_preset[0]["sub_type_reason"].length == 0){
                    if (DisconSchedule.showTableSchedule) $('.discon-schedule-alert').addClass('active');
                    $('#showCurOutage>p')[0].innerHTML = DisconSchedule.messages.get('no-blackout');
                    $('#showCurOutage').addClass('active');
                    DisconSchedule.tableHidden();
                }
                else {
                    DisconSchedule.tableRender(key_preset[0]["sub_type_reason"][0]);
                }
                DisconSchedule.disableWrapper('house_num');
                inp.value=' ';
                DisconSchedule.alertMessageBlock(key_preset,0);
                return;
            }
            currentFocus = -1;
            a = document.createElement("DIV");
            a.setAttribute("id", this.id + "autocomplete-list");
            a.setAttribute("class", "autocomplete-items");
            this.parentNode.appendChild(a);
            DisconSchedule.finded_count = 0;
            for (let i_index = 0; i_index < data.length; i_index++) {
                data_val = data[i_index].toLowerCase();
                val_val = val.toLowerCase();
                if (data_val.includes(val_val)) { //We can replace 'include' to 'replace' from function down here and it will be more quickly
                    b = document.createElement("DIV");
                    let new_value_elem = data_val.replace(val_val, "<strong>" + val_val + "</strong>"); //HARD Time Code
                    b.innerHTML = new_value_elem;
                    if (key_preset !== false && key_preset[i_index]["sub_type_reason"].length != 0) {
                        b.innerHTML += '<input type="hidden" value="' + data[i_index] + '" data-key-group="' + key_preset[i_index]["sub_type_reason"] + '">';
                    }
                    else {
                        b.innerHTML += '<input type="hidden" value="' + data[i_index] + '">';
                    }
                    b.onclick = function (e) {
                        inp.value = this.getElementsByTagName("input")[0].value;
                        if (inp.id == 'city') {
                            $('#discon_form #street').prop('disabled', false).addClass('active');
                            $('#discon_form #city')[0].closest('div.discon-input-wrapper').classList.add('preloader-spinner');
                            DisconSchedule.autocomplete($('#discon_form #street')[0], DisconSchedule.streets[inp.value], false);
                            $('#discon_form #city')[0].closest('div.discon-input-wrapper').classList.remove('preloader-spinner');
                            return;
                        }
                        DisconSchedule.selectelem = true;
                        if (key_preset !== false) {
                            if (DisconSchedule.form.data('old')!=inp.value) {
                                DisconSchedule.form.removeData('activeTab');
                                DisconSchedule.form.data('old', inp.value);
                            }
                            if (key_preset[i_index]["sub_type_reason"].length != 0) {
                                inp.setAttribute("data-key-group", this.getElementsByTagName("input")[0].getAttribute("data-key-group"));
                                $('#showCurOutage').removeClass('active');
                                $('.discon-schedule-table').addClass('active');
                                if (DisconSchedule.showTableSchedule) $('.discon-schedule-title').addClass('active');
                                if (DisconSchedule.showCurSchedule && DisconSchedule.showTableSchedule) $('#legendarium-table').addClass('active');
                            }
                            DisconSchedule.alertMessageBlock(key_preset,i_index,this);
                        }
                        else {
                            DisconSchedule.form.removeData('old');
                            DisconSchedule.ajax.formSubmit('getHomeNum');
                        }
                        closeAllLists();
                    };

                    a.appendChild(b);
                    $(this)[0].closest('.discon-input-wrapper').classList.add('open');
                    DisconSchedule.finded_count++;
                }
            }
        };
        inp.oninput = inputFunc;
        inp.onblur = (event) => {
            if (event.relatedTarget == $('.discon-schedule-table')[0]) {
                closeAllLists();
                $('.discon-schedule-table')[0].blur();
                event.stopPropagation();
            }
            if (!DisconSchedule.selectelem) {
                if ((inp.name == "street" || inp.name == "city") && inp.value != '') DisconSchedule.checkUpdateTimeout(null);
                inp.value = '';
                $('.discon-schedule-alert').removeClass('active');
                DisconSchedule.tableRender(0);
                $('#discon_form #group-name').css("display", "none");
                if (inp.name == "street" || inp.name == "city") {
                    DisconSchedule.disableWrapper('house_num');
                    if (inp.name == "city"){
                        DisconSchedule.disableWrapper('street');
                    }
                }
                $('#showCurOutage').removeClass('active');
            }
        };
        inp.onkeyup = function (e) {
            if ($('#discon_form #house_num')[0].value == "" || $('#discon_form #house_num')[0].value.trim() == "" || DisconSchedule.finded_count == 0) {
                $('#discon_form #house_num')[0].closest(".discon-input-wrapper").classList.add('error-active');
            }
            else {
                $('#discon_form #house_num')[0].closest(".discon-input-wrapper").classList.remove('error-active');
            }
            if (inp.name == "city") {
                DisconSchedule.disableWrapper('street');
                DisconSchedule.disableWrapper('house_num');

                if ($('#discon_form #city')[0].value == "" || $('#discon_form #city')[0].value.trim() == "" || DisconSchedule.finded_count == 0) {
                    $('#discon_form #city')[0].closest(".discon-input-wrapper").classList.add('error-active');
                }
                else {
                    $('#discon_form #city')[0].closest(".discon-input-wrapper").classList.remove('error-active');
                }
            }
            else
                if (inp.name == "street") {
                    DisconSchedule.disableWrapper('house_num');
                    if ($('#discon_form #street')[0].value == "" || $('#discon_form #street')[0].value.trim() == "" || DisconSchedule.finded_count == 0) {
                        $('#discon_form #street')[0].closest(".discon-input-wrapper").classList.add('error-active');
                    }
                    else {
                        $('#discon_form #street')[0].closest(".discon-input-wrapper").classList.remove('error-active');
                    }
                }
            $('.discon-schedule-alert').removeClass('active');
        };
        inp.onkeydown = function (e) {
            DisconSchedule.timeoutNotUse = Date.now();
            var x = document.getElementById(this.id + "autocomplete-list");
            DisconSchedule.tableRender(0);
            $('#showCurOutage').removeClass('active');
            $('#discon_form #group-name').css("display", "none");
            if (x) x = x.getElementsByTagName("div");
            if (e.keyCode == 40) {
                /*If the arrow DOWN key is pressed,
                increase the currentFocus variable:*/
                currentFocus++;
                /*and and make the current item more visible:*/
                addActive(x);
            } else if (e.keyCode == 38) { //up
                /*If the arrow UP key is pressed,
                decrease the currentFocus variable:*/
                currentFocus--;
                /*and and make the current item more visible:*/
                addActive(x);
            } else if (e.keyCode == 13) {
                /*If the ENTER key is pressed, prevent the form from being submitted,*/
                e.preventDefault();
                if (currentFocus > -1) {
                    /*and simulate a click on the "active" item:*/
                    if (x) {
                        x[currentFocus].click();
                        e.target.blur();
                        e.stopPropagation();
                    }
                }
                else {
                    if ($('.discon-input-wrapper.open')[0]) {
                        if ($('.discon-input-wrapper.open')[0]) $('.discon-input-wrapper.open').find('.autocomplete-items').find('div:first-child')[0].click();
                        e.target.blur();
                        e.stopPropagation();
                    }
                }
            }
        };
        function addActive(x) {
            if (!x) return false;
            removeActive(x);
            if (currentFocus >= x.length) currentFocus = 0;
            if (currentFocus < 0) currentFocus = (x.length - 1);
            x[currentFocus].addClass("autocomplete-active");
        }
        function removeActive(x) {
            for (var i = 0; i < x.length; i++) {
                x[i].removeClass("autocomplete-active");
            }
        }
        function closeAllLists(elmnt) {
            $('.discon-input-wrapper').removeClass('open');
            var x = document.getElementsByClassName("autocomplete-items");
            for (var i = 0; i < x.length; i++) {
                if (elmnt != x[i] && elmnt != inp) {
                    x[i].parentNode.removeChild(x[i]);
                }
            }
        }
        document.onclick = function (e) {
            closeAllLists(e.target);
        };
    },
    listopen_input: function (e) {
        var x = document.getElementsByClassName("autocomplete-items");
        for (var i = 0; i < x.length; i++) {
            x[i].parentNode.removeChild(x[i]);
        }
        DisconSchedule.selectelem = false;
        e.bubbles = false;
        if (e.target.value != '') {
            e.target.dispatchEvent(new Event('input', { bubbles: false }));
            e.stopPropagation();
        }
    },
    listopen: function (e) {
        var x = document.getElementsByClassName("autocomplete-items");
        for (var i = 0; i < x.length; i++) {
            x[i].parentNode.removeChild(x[i]);
        }
        e.bubbles = false;
        DisconSchedule.selectelem = false;
        e.target.parentNode.getElementsByTagName("input")[0].dispatchEvent(new Event('input', { bubbles: false }));
        e.target.parentNode.getElementsByTagName("input")[0].focus();
        e.stopPropagation();
    },
    tableHidden: function(e){
        $('.discon-schedule-table').removeClass('active');
        $('.discon-schedule-title').removeClass('active');
        $('#legendarium-table').removeClass('active');
        $('#discon_form #group-name').css("display", "none");
    },
    disableWrapper: function (element_id) {
        $(`#discon_form #${element_id}`)[0].classList.remove('active');
        $(`#discon_form #${element_id}`)[0].closest(".discon-input-wrapper").classList.remove('error-active');
        $(`#discon_form #${element_id}`).prop('disabled', true);
        $(`#discon_form #${element_id}`).val('');
    },
    enableWrapper: function (element_id) {
        $(`#discon_form #${element_id}`)[0].classList.add('active');
        $(`#discon_form #${element_id}`)[0].closest(".discon-input-wrapper").classList.add('error-active');
        $(`#discon_form #${element_id}`).prop('disabled', false);
    },
    alertMessageBlock: function(key_preset, index, in_list = false){
        if (key_preset[index]["sub_type_reason"].length > 1) {
            DisconSchedule.tableRender(0, true);
            if (DisconSchedule.showTableSchedule && DisconSchedule.showTablePlan) $('.discon-schedule-alert').addClass('active');
            else $('.discon-schedule-alert').removeClass('active');
        }
        else if (key_preset[index]["sub_type_reason"].length == 0) {
            DisconSchedule.tableHidden();
        }
        else {
            $('.discon-schedule-alert').removeClass('active');
            if ($('.discon-schedule-table').hasClass('active')){
                if(in_list) DisconSchedule.tableRender(in_list.getElementsByTagName("input")[0].getAttribute("data-key-group"));
                else DisconSchedule.tableRender(key_preset[0]["sub_type_reason"][0]);
            }
        }
        if (DisconSchedule.showCurOutage) {
            let noBlackoutMessage;
            if (key_preset[index]["sub_type"] == '' && key_preset[index]["start_date"] == '' && key_preset[index]["end_date"] == '') {
                noBlackoutMessage = DisconSchedule.messages.get('no-blackout');
                if (key_preset[index]["voluntarily"]) {
                    noBlackoutMessage += `<br>${DisconSchedule.messages.get('under-discon')}`;
                    DisconSchedule.tableHidden();
                    $('#discon-fact').html('');
                }
                noBlackoutMessage += `<br>${DisconSchedule.messages.get('extra-message')}`;
                $('#showCurOutage>p')[0].innerHTML = noBlackoutMessage;
            }
            else {
                let dateStartFormat = key_preset[index]["start_date"];
                let dateEndFormat = key_preset[index]["end_date"];
                switch (key_preset[index]["type"]) {
                    case "1":
                        $('#showCurOutage>p')[0].innerHTML = "За вашою адресою в даний момент відсутня електроенергія</br>Причина: <strong>планові ремонтні роботи</strong></br>Час початку – <strong>" + dateStartFormat + "</strong></br>Орієнтовний час відновлення електроенергії – <strong>до " + dateEndFormat + "</strong>";
                        break;
                    case "2":
                        $('#showCurOutage>p')[0].innerHTML = "За вашою адресою в даний момент відсутня електроенергія</br>Причина: <strong>" + key_preset[index]["sub_type"] + "</strong></br>Час початку – <strong>" + dateStartFormat + "</strong></br>Орієнтовний час відновлення електроенергії – <strong>до " + dateEndFormat + "</strong>";
                        break;
                    default:
                        break;
                }
            }
            if (key_preset[index]["sub_type_reason"] != '' && key_preset[index]["sub_type_reason"].length != 0 && key_preset[index]["sub_type"] != '' && key_preset[index]["voluntarily"]) {
                $('#showCurOutage>p')[0].innerHTML = $('#showCurOutage>p')[0].innerHTML + "<br><br>" + DisconSchedule.messages.get('under-discon');
                DisconSchedule.tableHidden();
                $('#discon-fact').html('');
            }
            else if (key_preset[index]["sub_type_reason"] == '' || key_preset[index]["sub_type_reason"].length == 0) {
                DisconSchedule.tableHidden();
            }
            if ($('#showCurOutage>p span._update_info').length==0) $('#showCurOutage>p')[0].innerHTML = $('#showCurOutage>p')[0].innerHTML + "<br><br><span class='_update_info'>Дата оновлення інформації</span> – " + DisconSchedule.updateTimestamp;
            $('#showCurOutage').addClass('active');
        }
    },
    tableRender: function (key_preset, multiGroup = false) {
        if (key_preset !=0 && !DisconSchedule.preset['sch_names'][key_preset]) {
            DisconSchedule.tableHidden();
            return;
        }
        if (!DisconSchedule.showCurSchedule) return;
        if (key_preset != 0 && key_preset !== false && $('#discon_form #house_num').val() != " ") $('#discon_form #house_num')[0].closest('div.discon-input-wrapper').classList.add('preloader-spinner');
        if (key_preset != 0 && key_preset !== false) $('#discon_form #group-name').css("display", "block");
        DisconSchedule.group = key_preset;
        DisconSchedule.multiGroup = multiGroup;
        $('#discon_form #group-name>span')[0].innerHTML = DisconSchedule.preset['sch_names'][key_preset];
        $('#house_num')[0].closest('div.discon-input-wrapper').classList.remove('preloader-spinner');
        var section = $($('.discon-fact')[0]);
        if (DisconSchedule.showTableFact && (key_preset || multiGroup)) {
            let active = true;
            if (DisconSchedule.form.data('activeTab')) active = DisconSchedule.form.data('activeTab');
            let block = $($('.template-discon-fact-blocks').html());
            section.html(block).addClass('active');
            let place = section.find('.discon-fact-tables');
            section.find('.discon-fact-info-text .update').text(DisconSchedule.fact.update);
            const today = new Date(DisconSchedule.fact.today * 1000);
            for (let offset = 0; offset<=1; offset++) {
                var div = $(section.find('.dates .date').get(offset));
                let day = new Date(today);
                day.setDate(today.getDate() + offset);
                day_text = day.toLocaleDateString('uk',{day:"2-digit", month: "2-digit", year: "2-digit", timeZone: "Europe/Kiev"});
                let key = (day.valueOf()/1000).toString();
                if (active===true) active = key;
                div.attr('rel',key).find('[rel=date]').text(day_text);
                if (multiGroup) {
                    var info = $('.discon-schedule-alert div').html();
                    var block_ins = $('<div/>',{class:'discon-info-message info-no-icon', rel:key}).append($('<div/>',{class:'info-div'}).html(info));
                    block_ins.find('.discon-info-text').text(DisconSchedule.messages.get('discon-multigroup'));
                    place.append(block_ins);
                }
                else if (DisconSchedule.fact.data && Object.keys(DisconSchedule.fact.data).length && DisconSchedule.fact.data[key]) {
                    var tables = [], tbody_fact = [];
                    if (window.innerWidth <= 845) {
                        $('.discon_schedule').removeClass('schedule-desktop').addClass('schedule-mobile');
                        var time_zone_keys = Object.keys(DisconSchedule.preset['time_zone']);
                        let half = time_zone_keys.length / 2;
                        for (var r = 0; r < half; r++) {
                            for (var x = 0; x <= 1; x++) {
                                if (typeof tables[x] == 'undefined') {
                                    tables[x] = $('<table/>');
                                    tables[x].append($('<thead/>').append($('<tr/>').append($('<th/>',{colspan:2,text:'Час'})).append($('<th/>'))));
                                    tbody_fact[x] = $('<tbody/>');
                                    tables[x].append(tbody_fact[x]);
                                }
                                i = x * half + r;
                                let day_class = 'cell-non-scheduled';
                                if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="no") day_class = 'cell-scheduled';
                                else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="maybe") day_class = 'cell-scheduled';
                                else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="first") day_class = 'cell-first-half';
                                else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="mfirst") day_class = 'cell-first-half';
                                else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="second") day_class = 'cell-second-half';
                                else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="msecond") day_class = 'cell-second-half';
                                let tz_text = DisconSchedule.preset['time_zone'][time_zone_keys[i]];
                                if (Array.isArray(tz_text)) tz_text = tz_text[0];
                                tbody_fact[x].append($('<tr/>').append($('<td/>',{colspan:2,text:tz_text})).append($('<td/>',{class:day_class})));
                            }
                        }
                        var block_ins = $('<div/>',{rel:key, class: 'discon-fact-table'});
                        block_ins.append($('<div/>',{class:'table2col'}).append(tables[0]).append(tables[1]));
                    }
                    else
                    {
                        $('.discon_schedule').removeClass('schedule-mobile').addClass('schedule-desktop');
                        let thead_tr = $('<tr/>');
                        let tbody_tr = $('<tr/>');
                        thead_tr.append($('<th/>',{colspan:2}).html('Часові<br>проміжки'));
                        tbody_tr.append($('<td/>',{colspan:2}).html('&nbsp;'));
                        var time_zone_keys = Object.keys(DisconSchedule.preset['time_zone']);
                        for (var i = 0; i < time_zone_keys.length; i++) {
                            let tz_text = DisconSchedule.preset['time_zone'][time_zone_keys[i]];
                            if (Array.isArray(tz_text)) tz_text = tz_text[0];
                            thead_tr.append($('<th/>',{scope:'col'}).append($('<div/>').html(tz_text) ));
                            let cell = $('<td/>');
                            tbody_tr.append(cell);
                            let day_class = 'cell-non-scheduled';
                            if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="no") day_class = 'cell-scheduled';
                            else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="maybe") day_class = 'cell-scheduled';
                            else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="first") day_class = 'cell-first-half';
                            else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="mfirst") day_class = 'cell-first-half';
                            else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="second") day_class = 'cell-second-half';
                            else if (DisconSchedule.fact.data[key][key_preset][time_zone_keys[i]]=="msecond") day_class = 'cell-second-half';
                            cell.addClass(day_class);
                        }
                        var block_ins = $('<div/>',{rel:key, class: 'discon-fact-table'}).append($('<table/>').append($('<thead/>').append(thead_tr)).append($('<tbody/>').append(tbody_tr)));
                    }
                    let legend = $($('.template-discon-fact-legend').html());
                    legend.find('.legend-item>#schedulled-text')[0].innerHTML = DisconSchedule.preset['time_type']['no'];
                    legend.find('.legend-item>#non-schedulled-text')[0].innerHTML = DisconSchedule.preset['time_type']['yes'];
                    legend.find('.legend-item>#first-schedulled-text')[0].innerHTML = DisconSchedule.preset['time_type']['first'];
                    legend.find('.legend-item>#second-schedulled-text')[0].innerHTML = DisconSchedule.preset['time_type']['second'];
                    block_ins.append(legend);
                    place.append(block_ins);
                }
                else {
                  if (today.valueOf() == day.valueOf()) {
                      var info = DisconSchedule.messages.get('discon-today-empty');
                  }
                  else {
                      var info = DisconSchedule.messages.get('discon-tomorrow-empty');
                  }
                  var block_ins = $('<div/>',{class:'discon-info-message', rel:key}).append($('<div/>',{text:info}));
                  if (today.valueOf() != day.valueOf()) { block_ins.addClass('info-tomorrow');}
                  place.append(block_ins);
                }
                if (active == key) {
                    div.addClass('active');
                    block_ins.addClass('active');
                }
            }
        }
        else {
            section.html('').removeClass('active');
        }
        const parent_table = document.getElementById('tableRenderElem'),
            table = document.createElement('table'),
            thead = document.createElement('thead'),
            tbody = document.createElement('tbody');
        parent_table.innerHTML = '';
        $('.discon-schedule-title').removeClass('active');
        $('#legendarium-table').removeClass('active');
        if (!DisconSchedule.showTableSchedule) { return;}
        if (DisconSchedule.showTablePlan && (key_preset == 0 || Object.keys(DisconSchedule.preset['data']).length)) {
            const tr_thead = thead.insertRow();
            if(window.innerWidth <= 845){
                tr_thead.insertCell().outerHTML = "<th colspan='2'><div class='head-time'>Час</div></th>";
                $('.discon_schedule').removeClass('schedule-desktop').addClass('schedule-mobile');
                for (var i = 1; i <= Object.keys(DisconSchedule.preset['days']).length; i++) {
                    if (parseInt(i) == DisconSchedule.currentWeekDayIndex) {
                        tr_thead.insertCell().outerHTML = "<th scope='col' class='current-day'><div>" + DisconSchedule.preset['days_mini'][i] + "</div></th>";
                    }
                    else {
                        tr_thead.insertCell().outerHTML = "<th scope='col'><div>" + DisconSchedule.preset['days_mini'][i] + "</div></th>";
                    }
                }
                table.appendChild(thead);
                var time_zone_keys = Object.keys(DisconSchedule.preset['time_zone']);
                for (var i = 0; i < time_zone_keys.length; i++) {
                    const tr_tbody = tbody.insertRow();
                    let tbody_content_html = "<td colspan='2'";
                    if (DisconSchedule.currentWeekDayIndex == 1){
                        tbody_content_html += " class='monday-td-day'";
                    }
                    let tz_text = DisconSchedule.preset['time_zone'][time_zone_keys[i]];
                    if (Array.isArray(tz_text)) tz_text = tz_text[0];
                    tbody_content_html += "><div>" + tz_text + "</div></td>";
                    tr_tbody.insertCell().outerHTML = tbody_content_html;
                    for (var j = 1; j <= Object.keys(DisconSchedule.preset['days']).length; j++) {
                        let yesterday_class = '';
                        if ((parseInt(j) + 1) == DisconSchedule.currentWeekDayIndex) { yesterday_class = "yesterday-cell"; }
                        else if (parseInt(j) == 1 && DisconSchedule.currentWeekDayIndex == 1) { yesterday_class = "monday-cell"; }
                        if (key_preset != 0) {
                            if (DisconSchedule.preset['data'][key_preset][j]) {
                                if (DisconSchedule.preset['data'][key_preset][j][time_zone_keys[i]]){
                                    switch (DisconSchedule.preset['data'][key_preset][j][time_zone_keys[i]]) {
                                        case "yes":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-non-scheduled "+ yesterday_class +"'></td>";
                                            break;
                                        case "maybe":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-scheduled-maybe "+ yesterday_class +"'></td>";
                                            break;
                                        case "no":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-scheduled "+ yesterday_class +"'></td>";
                                            break;
                                        case "first":
                                        case "mfirst":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-first-half "+ yesterday_class +"'></td>";
                                            break;
                                        case "second":
                                        case "msecond":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-second-half "+ yesterday_class +"'></td>";
                                            break;
                                        default:
                                            break;
                                    }
                                }
                                else {
                                    tr_tbody.insertCell().outerHTML = "<td class='cell-non-scheduled "+ yesterday_class +"'></td>";
                                }
                            }
                            else {
                                tr_tbody.insertCell().outerHTML = "<td class='cell-non-scheduled "+ yesterday_class +"'></td>";
                            }
                        }
                        else {
                            tr_tbody.insertCell().outerHTML = "<td class='cell-non-scheduled "+ yesterday_class +"'></td>";
                        }
                    }
                }
            }
            else {
                tr_thead.insertCell().outerHTML = "<th colspan='2'><div class='head-time'>Часові<br>проміжки</div></th>";
                $('.discon_schedule').removeClass('schedule-mobile').addClass('schedule-desktop');
                var time_zone_keys = Object.keys(DisconSchedule.preset['time_zone']);
                for (var i = 0; i < time_zone_keys.length; i++) {
                    let thead_content_html = "<th scope='col'";
                    if (DisconSchedule.currentWeekDayIndex == 1){
                        thead_content_html += " class='monday-th-day'";
                    }
                    let tz_text = DisconSchedule.preset['time_zone'][time_zone_keys[i]];
                    if (Array.isArray(tz_text)) tz_text = tz_text[0];
                    thead_content_html += "><div>" + tz_text + "</div></th>";
                    tr_thead.insertCell().outerHTML = thead_content_html;
                }
                table.appendChild(thead);
                for (var i = 1; i <= Object.keys(DisconSchedule.preset['days']).length; i++) {
                    const tr_tbody = tbody.insertRow();
                    if (parseInt(i) == DisconSchedule.currentWeekDayIndex) {
                        tr_tbody.insertCell().outerHTML = "<td colspan='2' class='current-day'><div>" + DisconSchedule.preset['days'][i] + "</div></td>";
                    }
                    else{
                        if ((parseInt(i) + 1) == DisconSchedule.currentWeekDayIndex) tr_tbody.className = "yesterday-row";
                        tr_tbody.insertCell().outerHTML = "<td colspan='2'><div>" + DisconSchedule.preset['days'][i] + "</div></td>";
                    }
                    if (parseInt(i) == 1 && DisconSchedule.currentWeekDayIndex == 1) tr_tbody.className = "monday-row";
                    for (var j = 0; j < time_zone_keys.length; j++) {
                        if (key_preset != 0) {
                            if (DisconSchedule.preset['data'][key_preset][i]) {
                                if (DisconSchedule.preset['data'][key_preset][i][time_zone_keys[j]]){
                                    switch (DisconSchedule.preset['data'][key_preset][i][time_zone_keys[j]]) {
                                        case "yes":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-non-scheduled'></td>";
                                            break;
                                        case "maybe":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-scheduled-maybe'></td>";
                                            break;
                                        case "no":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-scheduled'></td>";
                                            break;
                                        case "first":
                                        case "mfirst":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-first-half'></td>";
                                            break;
                                        case "second":
                                        case "msecond":
                                            tr_tbody.insertCell().outerHTML = "<td class='cell-second-half'></td>";
                                            break;
                                        default:
                                            break;
                                    }
                                }
                                else {
                                    tr_tbody.insertCell().outerHTML = "<td class='cell-non-scheduled'></td>";
                                }
                            }
                            else {
                                tr_tbody.insertCell().outerHTML = "<td class='cell-non-scheduled'></td>";
                            }
                        }
                        else {
                            tr_tbody.insertCell().outerHTML = "<td class='cell-non-scheduled'></td>";
                        }
                    }
                }
            }
            table.appendChild(tbody);
            parent_table.appendChild(table);
            $('.discon-schedule-table').addClass('active');
            if (DisconSchedule.showTableSchedule) $('.discon-schedule-title').addClass('active');
            $('#legendarium-table').addClass('active');
        }
        if (key_preset != 0 && key_preset !== false) $('#house_num')[0].closest('div.discon-input-wrapper').classList.remove('preloader-spinner');
    },
    ajax: {
        url: undefined, obj: {}, send: function (success, failure) {
            $.post(this.url, this.obj, (answer) => {
                this.obj = {};
                answer.result && typeof success === 'function' ? success(answer) :
                    !answer.result && typeof failure === 'function' ? failure(answer) : null;
                DisconSchedule.ajax.obj = {};
            }, 'JSON');
        },
        getStreetsInvisibly: function () {
            this.obj.method = 'getStreets';
            this.send(function (answer) {
                if (Object.keys(answer.streets).length) {
                    DisconSchedule.streets = answer.streets;
                    DisconSchedule.autocomplete($('#discon_form #city')[0], Object.keys(DisconSchedule.streets), false);
                    if ($(':focus').prop('name')=='city' && document.getSelection().anchorNode.className == "autocomplete" && document.getSelection().toString()=="") {
                        $('#discon_form #city')[0].click();
                        if (DisconSchedule.finded_count) $('#discon_form .error-active').removeClass('error-active');
                        if (!$('#discon_form #street').val().trim()) {
                            DisconSchedule.disableWrapper('street');
                        }
                        return;
                    }
                    if ($('#discon_form #city').val()) {
                        if (!Object.keys(DisconSchedule.streets).includes($('#discon_form #city').val())) {
                                DisconSchedule.autocomplete($('#discon_form #street')[0], ['.'], [{'sub_type_reason':[]}]);
                                DisconSchedule.autocomplete($('#discon_form #house_num')[0], ['.'], [{'sub_type_reason':[]}]);
                                $('#discon_form #street').closest('.discon-input-wrapper').removeClass('open');
                                $('.autocomplete-items').remove();
                                if (!$('#discon_form #house_num').val().trim()) {
                                    DisconSchedule.disableWrapper('house_num');
                                }
                                if (!$('#discon_form #street').val().trim()) {
                                    DisconSchedule.disableWrapper('street');
                                }
                                if (($(':focus').prop('name')=='street' || $(':focus').prop('name')=='house_num')
                                    && document.getSelection().anchorNode.className == "autocomplete" && document.getSelection().toString()=="") {
                                    return;
                                }
                                $('#showCurOutage>p')[0].innerHTML = DisconSchedule.messages.get('no-blackout');
                                $('#showCurOutage>p')[0].innerHTML += `<br>${DisconSchedule.messages.get('extra-message')}`;
                                $('#showCurOutage').addClass('active');
                                $('#discon-fact').html('');
                                DisconSchedule.tableHidden();
                                return;
                        }
                        if ($('#discon_form #street').prop('disabled')) {
                            if (DisconSchedule.streets[$('#discon_form #city').val()].length==1 && DisconSchedule.streets[$('#discon_form #city').val()][0]=="") {
                              DisconSchedule.ajax.getHomeNumInvisibly();
                              return;
                            }
                            else if (DisconSchedule.streets[$('#discon_form #city').val()].length>0) {
                                DisconSchedule.enableWrapper('street');
                                $('#discon_form #street').val('');
                                DisconSchedule.autocomplete($('#discon_form #street')[0], DisconSchedule.streets[$('#discon_form #city').val()], false);
                                $('#discon_form #group-name').css("display", "none");
                                $('#showCurOutage').removeClass('active');
                                $('#discon-fact').html('');
                                DisconSchedule.tableHidden();
                                DisconSchedule.tableRender(0);
                            }
                        } else {
                            if (DisconSchedule.streets[$('#discon_form #city').val()].length==1 && DisconSchedule.streets[$('#discon_form #city').val()][0]=="") {
                                if (!$('#discon_form #street').val().trim()) {
                                    DisconSchedule.disableWrapper('street');
                                    DisconSchedule.ajax.getHomeNumInvisibly();
                                    return;
                                }
                                DisconSchedule.autocomplete($('#discon_form #street')[0], ['.'], [{'sub_type_reason':[]}]);
                            }
                            else
                                DisconSchedule.autocomplete($('#discon_form #street')[0], DisconSchedule.streets[$('#discon_form #city').val()], false);
                            if ($(':focus').prop('name')=='street' && document.getSelection().anchorNode.className == "autocomplete" && document.getSelection().toString()=="") {
                                $('#discon_form #street').closest('.discon-input-wrapper').removeClass('open');
                                $('.autocomplete-items').remove();
                                $('#discon_form #street')[0].click();
                                if (DisconSchedule.finded_count) $('#discon_form .error-active').removeClass('error-active');
                                return;
                            }
                        }
                    }
                    else
                        $('.autocomplete-items').remove();
                    if ($('#discon_form #street').val()) {
                        DisconSchedule.ajax.getHomeNumInvisibly();
                    }
                }
            },
            function (answer) {
            });
        },
        getHomeNumInvisibly: function () {
            this.obj.method = 'getHomeNum';
            this.obj.data = DisconSchedule.form.serializeArray();
            this.send(function (answer) {
                DisconSchedule.showCurOutage = answer.showCurOutageParam;
                DisconSchedule.showCurSchedule = answer.showCurSchedule;
                DisconSchedule.showTableSchedule = answer.showTableSchedule;
                DisconSchedule.showTablePlan = answer.showTablePlan;
                DisconSchedule.showTableFact = answer.showTableFact;
                DisconSchedule.updateTimestamp = answer.updateTimestamp;
                if (answer.fact) {
                    DisconSchedule.fact = answer.fact;
                    DisconSchedule.form.find('[name=updateFact]').val(DisconSchedule.fact.update);
                    DisconSchedule.preset = answer.preset;
                }
                if ((DisconSchedule.fact.data.length==0 && DisconSchedule.preset.data.length==0) || DisconSchedule.preset.time_zone.length==0) {
                    DisconSchedule.showCurSchedule = false;
                    DisconSchedule.showTableFact = false;
                    DisconSchedule.showTableSchedule = false;
                }
                if (answer.data) {
                    let data_home_num = [];
                    let data_home_group = [];
                    $.each(answer.data, (k, v) => {
                        data_home_num.push(k);
                        data_home_group.push(v);
                    });
                    if (!DisconSchedule.showTableSchedule) {
                        $('#tableRenderElem').html('');
                        $('#legendarium-table').removeClass('active');
                    }
                    if (!DisconSchedule.showCurOutage)
                        $('#showCurOutage').removeClass('active');
                    if (data_home_num[0] == '-' && data_home_num.length==1) {
                        if ($(':focus').prop('name')=='house_num' && document.getSelection().anchorNode.className == "autocomplete" && document.getSelection().toString()=="") {
                            DisconSchedule.autocomplete($('#discon_form #house_num')[0], ['.'], data_home_group);
                            $('#discon_form #house_num').closest('.discon-input-wrapper').removeClass('open');
                            $('.autocomplete-items').remove();
                            return;
                        }
                        if (data_home_group[0]["sub_type_reason"].length > 1) {
                            $('#discon_form #group-name').css("display", "none");
                            $('#discon-fact').html('');
                            DisconSchedule.tableRender(0);
                            if (DisconSchedule.showTableSchedule) $('.discon-schedule-alert').addClass('active');
                        }
                        else if(data_home_group[0]["sub_type_reason"].length == 0){
                            if (DisconSchedule.showTableSchedule) $('.discon-schedule-alert').addClass('active');
                            $('#discon-fact').html('');
                            DisconSchedule.tableHidden();
                        }
                        else {
                            $('#discon-fact').html('');
                            DisconSchedule.tableRender(data_home_group[0]["sub_type_reason"][0]);
                        }
                        if (!$('#discon_form #house_num').prop('disabled') && !$('#discon_form #house_num').val().trim()) {
                            DisconSchedule.disableWrapper('house_num');
                            $('#discon_form #house_num')[0].value = " ";
                            $('#discon_form #house_num').prop('disabled', true);
                            $('#discon_form #house_num').removeClass('active');
                            $('#discon_form .error-active').removeClass('error-active');
                            $('#showCurOutage').removeClass('active');
                            $('.discon-schedule-table').addClass('active');
                            $('.discon-schedule-title').addClass('active');
                            if (DisconSchedule.showCurSchedule) $('#legendarium-table').addClass('active');
                            DisconSchedule.alertMessageBlock(data_home_group,0);
                            return;
                        }
                        if (!$('#discon_form #house_num').closest('.discon-input-wrapper').hasClass('error-active')
                            && !$('#discon_form #house_num').prop('disabled')) {
                            DisconSchedule.autocomplete($('#discon_form #house_num')[0], ['.'], data_home_group);
                            $('#showCurOutage>p')[0].innerHTML = DisconSchedule.messages.get('no-blackout');
                            $('#showCurOutage>p')[0].innerHTML += `<br>${DisconSchedule.messages.get('extra-message')}`;
                            $('#showCurOutage').addClass('active');
                            $('#discon-fact').html('');
                            DisconSchedule.tableHidden();
                            return;
                        }
                        DisconSchedule.alertMessageBlock(data_home_group,0);
                        return;
                    }
                    else {
                        if (($(':focus').prop('name')=='house_num' && document.getSelection().anchorNode.className == "autocomplete" && document.getSelection().toString()=="") || $('#discon_form #house_num').closest('.discon-input-wrapper').hasClass('error-active') || !$('#discon_form #house_num').val().trim()) {
                            if ($('#discon_form #house_num').prop('disabled')) {
                                DisconSchedule.enableWrapper('house_num');
                                $('#discon_form #house_num').val('');
                                $('#showCurOutage').removeClass('active');
                                DisconSchedule.tableHidden();
                                DisconSchedule.tableRender(0);
                                DisconSchedule.autocomplete($('#discon_form #house_num')[0], data_home_num, data_home_group);
                                return;
                            }
                            DisconSchedule.autocomplete($('#discon_form #house_num')[0], data_home_num, data_home_group);
                            if ($(':focus').prop('name')=='house_num' && document.getSelection().anchorNode.className == "autocomplete" && document.getSelection().toString()=="") {
                                $('#discon_form #house_num')[0].click();
                                if (DisconSchedule.finded_count) $('#discon_form .error-active').removeClass('error-active');
                            }
                            return;
                        }
                    }
                    if ($('#discon_form #house_num').val()) {
                        DisconSchedule.autocomplete($('#discon_form #house_num')[0], data_home_num, data_home_group);
                        if (answer.data[$('#discon_form #house_num').val()]) {
                            var key_preset_one = answer.data[$('#discon_form #house_num').val()];
                            var key_presets = [key_preset_one];
                            if (key_preset_one["sub_type_reason"].length > 1) {
                                $('#discon_form #group-name').css("display", "none");
                                DisconSchedule.tableRender(0, true);
                                if (DisconSchedule.showTableSchedule) $('.discon-schedule-alert').addClass('active');
                            }
                            else if(key_preset_one["sub_type_reason"].length == 0){
                                if (DisconSchedule.showTableSchedule) $('.discon-schedule-alert').addClass('active');
                                DisconSchedule.tableHidden();
                                $('#discon-fact').html('');
                            }
                            else {
                                $('#discon-fact').html('');
                                DisconSchedule.tableRender(key_preset_one["sub_type_reason"][0]);
                            }
                            if (!DisconSchedule.showCurOutage) $('#showCurOutage').removeClass('active');
                            DisconSchedule.alertMessageBlock(key_presets,0);
                        }
                        else {
                            $('#showCurOutage>p')[0].innerHTML = DisconSchedule.messages.get('no-blackout');
                            $('#showCurOutage>p')[0].innerHTML += `<br>${DisconSchedule.messages.get('extra-message')}`;
                            $('#showCurOutage').addClass('active');
                            DisconSchedule.tableHidden();
                            $('#discon-fact').html('');
                        }
                    }
                }
            },
            function (answer) {
                $('#discon_form #house_num').closest('.discon-input-wrapper').removeClass('open');
                $('.autocomplete-items').remove();
                if ($('#discon_form #house_num').val()) {
                    DisconSchedule.autocomplete($('#discon_form #house_num')[0], ["."], [{'sub_type_reason':[]}]);
                }
                else {
                    DisconSchedule.disableWrapper('house_num');
                }
                if ($(':focus').prop('name')=='house_num' && document.getSelection().anchorNode.className == "autocomplete" && document.getSelection().toString()=="") return;
                $('#discon_form .error-active').removeClass('error-active');
                $('#showCurOutage>p')[0].innerHTML = DisconSchedule.messages.get('no-blackout');
                $('#showCurOutage>p')[0].innerHTML += `<br>${DisconSchedule.messages.get('extra-message')}`;
                $('#showCurOutage').addClass('active');
                DisconSchedule.tableHidden();
                $('#discon-fact').html('');
            });
        },
        checkDisconUpdate: function () {
            this.obj.method = 'checkDisconUpdate';
            this.obj.update = DisconSchedule.fact.update;
            this.send(function (answer) {
                if (answer.fact) {
                    DisconSchedule.fact = answer.fact;
                    DisconSchedule.form.find('[name=updateFact]').val(DisconSchedule.fact.update);
                    DisconSchedule.preset = answer.preset;
                }
                if (DisconSchedule.preset.data.length==0) {
                    DisconSchedule.showTableSchedule = false;
                }
                else {
                    DisconSchedule.showTableSchedule = answer.showTableSchedule;
                }
            },
            function (answer) {
            });
        },
        formSubmit: function (method) {
            $('#discon_form #street')[0].closest('div.discon-input-wrapper').classList.add('preloader-spinner');
            this.obj.method = method;
            this.obj.data = DisconSchedule.form.serializeArray();
            this.send(function (answer) {
                DisconSchedule.showCurOutage = answer.showCurOutageParam;
                DisconSchedule.showCurSchedule = answer.showCurSchedule;
                DisconSchedule.showTableSchedule = answer.showTableSchedule;
                DisconSchedule.showTablePlan = answer.showTablePlan;
                DisconSchedule.showTableFact = answer.showTableFact;
                DisconSchedule.updateTimestamp = answer.updateTimestamp;
                DisconSchedule.checkUpdateTimeout(true);
                DisconSchedule.form.removeData('activeTab');
                if (answer.fact) {
                    DisconSchedule.fact = answer.fact;
                    DisconSchedule.form.find('[name=updateFact]').val(DisconSchedule.fact.update);
                    DisconSchedule.preset = answer.preset;
                }
                if ((DisconSchedule.fact.data.length==0 && DisconSchedule.preset.data.length==0) || DisconSchedule.preset.time_zone.length==0) {
                    DisconSchedule.showCurSchedule = false;
                    DisconSchedule.showTableFact = false;
                    DisconSchedule.showTableSchedule = false;
                }
                let data_home_num = [];
                let data_home_group = [];
                $.each(answer.data, (k, v) => {
                    data_home_num.push(k);
                    data_home_group.push(v);
                });
                if (!DisconSchedule.showTableSchedule) {
                    $('#tableRenderElem').html('');
                    $('.discon-schedule-title').removeClass('active');
                    $('#legendarium-table').removeClass('active');
                }
                DisconSchedule.autocomplete($('#discon_form #house_num')[0], data_home_num, data_home_group);
                $('#discon_form #street')[0].closest('div.discon-input-wrapper').classList.remove('preloader-spinner');
                if ($('#discon_form #house_num')[0].value == "") {
                    DisconSchedule.enableWrapper('house_num');
                }
            },
            function (answer) {
                $('#discon_form #street')[0].closest('div.discon-input-wrapper').classList.remove('preloader-spinner');
                $('#discon_form #street').focus();
                DisconSchedule.disableWrapper('house_num');
                $('#discon_form .error-active').removeClass('error-active');
                $('#showCurOutage>p')[0].innerHTML = DisconSchedule.messages.get('no-blackout');// + '<br><br>' + DisconSchedule.messages.get('leave-message');
                $('#showCurOutage>p')[0].innerHTML += `<br>${DisconSchedule.messages.get('extra-message')}`;
                $('#showCurOutage').addClass('active');
                DisconSchedule.tableHidden();
            });
        }
    }
};
