const allSideMenu = document.querySelectorAll('#sidebar .side-menu.top li a');

// Set the active link based on the stored value in localStorage
const activePage = localStorage.getItem('activePage');
if (activePage) {
    allSideMenu.forEach(item => {
        const li = item.parentElement;
        if (item.getAttribute('href') === activePage) {
            li.classList.add('active');
        } else {
            li.classList.remove('active'); // Ensure other items are inactive
        }
    });
}

allSideMenu.forEach(item => {
    const li = item.parentElement;

    item.addEventListener('click', function () {
        // Remove active class from all items
        allSideMenu.forEach(i => {
            i.parentElement.classList.remove('active');
        });
        // Add active class to the clicked item
        li.classList.add('active');

        // Store the active page in localStorage
        localStorage.setItem('activePage', item.getAttribute('href'));
    });
});

// TOGGLE SIDEBAR
const menuBar = document.querySelector('#content nav .bx.bx-menu');
const sidebar = document.getElementById('sidebar');

menuBar.addEventListener('click', function () {
    sidebar.classList.toggle('hide');
});

// Search functionality
const searchButton = document.querySelector('#content nav form .form-input button');
const searchButtonIcon = document.querySelector('#content nav form .form-input button .bx');
const searchForm = document.querySelector('#content nav form');

searchButton.addEventListener('click', function (e) {
    if (window.innerWidth < 576) {
        e.preventDefault();
        searchForm.classList.toggle('show');
        if (searchForm.classList.contains('show')) {
            searchButtonIcon.classList.replace('bx-search', 'bx-x');
        } else {
            searchButtonIcon.classList.replace('bx-x', 'bx-search');
        }
    }
});

// Responsive behavior
if (window.innerWidth < 768) {
    sidebar.classList.add('hide');
} else if (window.innerWidth > 576) {
    searchButtonIcon.classList.replace('bx-x', 'bx-search');
    searchForm.classList.remove('show');
}

window.addEventListener('resize', function () {
    if (this.innerWidth > 576) {
        searchButtonIcon.classList.replace('bx-x', 'bx-search');
        searchForm.classList.remove('show');
    }
});

// Dark mode toggle
const switchMode = document.getElementById('switch-mode');

switchMode.addEventListener('change', function () {
    if (this.checked) {
        document.body.classList.add('dark');
    } else {
        document.body.classList.remove('dark');
    }
});
